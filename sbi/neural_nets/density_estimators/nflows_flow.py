from typing import Tuple

import torch
from pyknos.nflows.flows import Flow
from torch import Tensor, nn

from sbi.neural_nets.density_estimators.base import DensityEstimator
from sbi.sbi_types import Shape


class NFlowsFlow(DensityEstimator):
    r"""`nflows`- based normalizing flow density estimator.

    Flow type objects already have a .log_prob() and .sample() method, so here we just
    wrap them and add the .loss() method.
    """

    def __init__(self, net: Flow, condition_shape: torch.Size) -> None:
        super().__init__(net, condition_shape)
        # TODO: Remove as soon as DensityEstimator becomes abstract
        self.net: Flow

    @property
    def embedding_net(self) -> nn.Module:
        r"""Return the embedding network."""
        return self.net._embedding_net

    def inverse_transform(self, input: Tensor, condition: Tensor) -> Tensor:
        r"""Return the inverse flow-transform of the inputs given a condition.

        The inverse transform is the transformation that maps the inputs back to the
        base distribution (noise) space.

        Args:
            input: Inputs to evaluate the inverse transform on of shape
                    (*batch_shape1, input_size).
            condition: Conditions of shape (*batch_shape2, *condition_shape).

        Raises:
            RuntimeError: If batch_shape1 and batch_shape2 are not broadcastable.

        Returns:
            noise: Transformed inputs.

        Note:
            This function should support PyTorch's automatic broadcasting. This means
            the function should behave as follows for different input and condition
            shapes:
            - (input_size,) + (batch_size,*condition_shape) -> (batch_size,)
            - (batch_size, input_size) + (*condition_shape) -> (batch_size,)
            - (batch_size, input_size) + (batch_size, *condition_shape) -> (batch_size,)
            - (batch_size1, input_size) + (batch_size2, *condition_shape)
                                                  -> RuntimeError i.e. not broadcastable
            - (batch_size1,1, input_size) + (batch_size2, *condition_shape)
                                                  -> (batch_size1,batch_size2)
            - (batch_size1, input_size) + (batch_size2,1, *condition_shape)
                                                  -> (batch_size2,batch_size1)
        """
        self._check_condition_shape(condition)
        condition_dims = len(self._condition_shape)

        # PyTorch's automatic broadcasting
        batch_shape_in = input.shape[:-1]
        batch_shape_cond = condition.shape[:-condition_dims]
        batch_shape = torch.broadcast_shapes(batch_shape_in, batch_shape_cond)
        # Expand the input and condition to the same batch shape
        input = input.expand(batch_shape + (input.shape[-1],))
        condition = condition.expand(batch_shape + self._condition_shape)
        # Flatten required by nflows, but now both have the same batch shape
        input = input.reshape(-1, input.shape[-1])
        condition = condition.reshape(-1, *self._condition_shape)

        noise, _ = self.net._transorm(input, context=condition)
        noise = noise.reshape(batch_shape)
        return noise

    def log_prob(self, input: Tensor, condition: Tensor) -> Tensor:
        r"""Return the log probabilities of the inputs given a condition or multiple
        i.e. batched conditions.

        Args:
            input: Inputs to evaluate the log probability on. Of shape
                `(sample_dim, batch_dim, *event_shape)`.
            condition: Conditions of shape `(sample_dim, batch_dim, *event_shape)`.

        Raises:
            AssertionError: If `input_batch_dim != condition_batch_dim`.

        Returns:
            Sample-wise log probabilities, shape `(input_sample_dim, input_batch_dim)`.
        """
        input_sample_dim = input.shape[0]
        input_batch_dim = input.shape[1]
        condition_batch_dim = condition.shape[0]
        condition_event_dims = len(condition.shape[1:])

        assert condition_batch_dim == input_batch_dim, (
            f"Batch shape of condition {condition_batch_dim} and input "
            f"{input_batch_dim} do not match."
        )

        # Nflows needs to have a single batch dimension for condition and input.
        input = input.reshape((input_batch_dim * input_sample_dim, -1))

        # Repeat the condition to match `input_batch_dim * input_sample_dim`.
        ones_for_event_dims = (1,) * condition_event_dims  # Tuple of 1s, e.g. (1, 1, 1)
        condition = condition.repeat(input_sample_dim, *ones_for_event_dims)

        log_probs = self.net.log_prob(input, context=condition)
        return log_probs.reshape((input_sample_dim, input_batch_dim))

    def loss(self, input: Tensor, condition: Tensor) -> Tensor:
        r"""Return the loss for training the density estimator.

        Args:
            input: Inputs to evaluate the loss on of shape
                `(sample_dim, batch_dim, *event_shape)`.
            condition: Conditions of shape `(sample_dim, batch_dim, *event_dim)`.

        Returns:
            Negative log_probability of shape `(input_sample_dim, condition_batch_dim)`.
        """
        return -self.log_prob(input, condition)

    def sample(self, sample_shape: Shape, condition: Tensor) -> Tensor:
        r"""Return samples from the density estimator.

        Args:
            sample_shape: Shape of the samples to return.
            condition: Conditions of shape `(sample_dim, batch_dim, *event_shape)`.

        Returns:
            Samples of shape `(*sample_shape, condition_batch_dim)`.
        """
        condition_batch_dim = condition.shape[0]
        num_samples = torch.Size(sample_shape).numel()

        samples = self.net.sample(num_samples, context=condition)

        return samples.reshape((
            *sample_shape,
            condition_batch_dim,
            -1,
        ))

    def sample_and_log_prob(
        self, sample_shape: torch.Size, condition: Tensor, **kwargs
    ) -> Tuple[Tensor, Tensor]:
        r"""Return samples and their density from the density estimator.

        Args:
            sample_shape: Shape of the samples to return.
            condition: Conditions of shape (sample_dim, batch_dim, *event_shape).

        Returns:
            Samples of shape `(*sample_shape, condition_batch_dim, *input_event_shape)`
            and associated log probs of shape `(*sample_shape, condition_batch_dim)`.
        """
        condition_batch_dim = condition.shape[0]
        num_samples = torch.Size(sample_shape).numel()

        samples, log_probs = self.net.sample_and_log_prob(
            num_samples, context=condition
        )
        samples = samples.reshape((*sample_shape, condition_batch_dim, -1))
        log_probs = log_probs.reshape((*sample_shape, -1))
        return samples, log_probs
