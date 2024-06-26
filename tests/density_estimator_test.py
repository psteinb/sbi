# This file is part of sbi, a toolkit for simulation-based inference. sbi is licensed
# under the Affero General Public License v3, see <https://www.gnu.org/licenses/>.

from __future__ import annotations

from typing import Tuple

import pytest
import torch
from torch import eye, zeros
from torch.distributions import MultivariateNormal

from sbi.neural_nets import build_mnle
from sbi.neural_nets.categorial import build_categoricalmassestimator
from sbi.neural_nets.density_estimators.shape_handling import (
    reshape_to_sample_batch_event,
)
from sbi.neural_nets.embedding_nets import CNNEmbedding
from sbi.neural_nets.flow import (
    build_maf,
    build_maf_rqs,
    build_nsf,
    build_zuko_bpf,
    build_zuko_gf,
    build_zuko_maf,
    build_zuko_naf,
    build_zuko_ncsf,
    build_zuko_nice,
    build_zuko_nsf,
    build_zuko_sospf,
    build_zuko_unaf,
)
from sbi.neural_nets.mdn import build_mdn


def get_batch_input(nsamples: int, input_dims: int) -> torch.Tensor:
    r"""Generate a batch of input samples from a multivariate normal distribution.

    Args:
        nsamples (int): The number of samples to generate.
        input_dims (int): The dimensionality of the input samples.

    Returns:
        torch.Tensor: A tensor of shape (nsamples, input_dims)
        containing the generated samples.
    """
    input_mvn = MultivariateNormal(
        loc=zeros(input_dims), covariance_matrix=eye(input_dims)
    )
    return input_mvn.sample((nsamples,))


def get_batch_context(nsamples: int, condition_shape: tuple[int, ...]) -> torch.Tensor:
    r"""Generate a batch of context samples from a multivariate normal distribution.

    Args:
        nsamples (int): The number of context samples to generate.
        condition_shape (tuple[int, ...]): The shape of the condition for each sample.

    Returns:
        torch.Tensor: A tensor containing the generated context samples.
    """
    context_mvn = MultivariateNormal(
        loc=zeros(*condition_shape), covariance_matrix=eye(condition_shape[-1])
    )
    return context_mvn.sample((nsamples,))


@pytest.mark.parametrize(
    "theta_or_x_shape, target_shape, event_shape, leading_is_sample",
    (
        ((3,), (1, 1, 3), (3,), False),
        ((3,), (1, 1, 3), (3,), True),
        ((1, 3), (1, 1, 3), (3,), False),
        ((1, 3), (1, 1, 3), (3,), True),
        ((2, 3), (1, 2, 3), (3,), False),
        ((2, 3), (2, 1, 3), (3,), True),
        ((1, 2, 3), (1, 2, 3), (3,), True),
        ((1, 2, 3), (2, 1, 3), (3,), False),
        ((3, 5), (1, 1, 3, 5), (3, 5), False),
        ((3, 5), (1, 1, 3, 5), (3, 5), True),
        ((1, 3, 5), (1, 1, 3, 5), (3, 5), False),
        ((1, 3, 5), (1, 1, 3, 5), (3, 5), True),
        ((2, 3, 5), (1, 2, 3, 5), (3, 5), False),
        ((2, 3, 5), (2, 1, 3, 5), (3, 5), True),
        ((1, 2, 3, 5), (1, 2, 3, 5), (3, 5), True),
        ((1, 2, 3, 5), (2, 1, 3, 5), (3, 5), False),
        pytest.param((1, 2, 3, 5), (1, 2, 3, 5), (5), False, marks=pytest.mark.xfail),
        pytest.param((1, 2, 3, 5), (1, 2, 3, 5), (3), False, marks=pytest.mark.xfail),
        pytest.param((1, 2, 3), (1, 2, 3), (1, 5), False, marks=pytest.mark.xfail),
        pytest.param((1, 2, 3), (1, 2, 3), (1, 3), False, marks=pytest.mark.xfail),
        pytest.param((1, 2, 3), (2, 1, 3), (3), False, marks=pytest.mark.xfail),
        pytest.param((1, 2, 3), (2, 1, 3), (3), True, marks=pytest.mark.xfail),
    ),
)
def test_shape_handling_utility_for_density_estimator(
    theta_or_x_shape: Tuple,
    target_shape: Tuple,
    event_shape: Tuple,
    leading_is_sample: bool,
):
    """Test whether `reshape_to_batch_sample_event` results in expected outputs."""
    input = torch.randn(theta_or_x_shape)
    output = reshape_to_sample_batch_event(
        input, event_shape=event_shape, leading_is_sample=leading_is_sample
    )
    assert output.shape == target_shape, (
        f"Shapes of Output ({output.shape}) and target shape ({target_shape}) do not "
        f"match."
    )


@pytest.mark.parametrize(
    "density_estimator_build_fn",
    (
        build_mdn,
        build_maf,
        build_maf_rqs,
        build_nsf,
        build_zuko_bpf,
        build_zuko_gf,
        build_zuko_maf,
        build_zuko_naf,
        build_zuko_ncsf,
        build_zuko_nice,
        build_zuko_nsf,
        build_zuko_sospf,
        build_zuko_unaf,
        build_categoricalmassestimator,
        build_mnle,
    ),
)
@pytest.mark.parametrize("input_sample_dim", (1, 2))
@pytest.mark.parametrize("input_event_shape", ((1,), (4,)))
@pytest.mark.parametrize("condition_event_shape", ((1,), (7,)))
@pytest.mark.parametrize("batch_dim", (1, 10))
def test_density_estimator_loss_shapes(
    density_estimator_build_fn,
    input_sample_dim,
    input_event_shape,
    condition_event_shape,
    batch_dim,
):
    """Test whether `loss` of DensityEstimators follow the shape convention."""
    density_estimator, inputs, conditions = _build_density_estimator_and_tensors(
        density_estimator_build_fn,
        input_event_shape,
        condition_event_shape,
        batch_dim,
        input_sample_dim,
    )

    losses = density_estimator.loss(inputs, condition=conditions)
    assert losses.shape == (input_sample_dim, batch_dim)


@pytest.mark.parametrize(
    "density_estimator_build_fn",
    (
        build_mdn,
        build_maf,
        build_maf_rqs,
        build_nsf,
        build_zuko_bpf,
        build_zuko_gf,
        build_zuko_maf,
        build_zuko_naf,
        build_zuko_ncsf,
        build_zuko_nice,
        build_zuko_nsf,
        build_zuko_sospf,
        build_zuko_unaf,
        build_categoricalmassestimator,
    ),
)
@pytest.mark.parametrize("input_sample_dim", (1, 2))
@pytest.mark.parametrize("input_event_shape", ((1,), (4,)))
@pytest.mark.parametrize("condition_event_shape", ((1, 1), (1, 7), (7, 1), (7, 7)))
@pytest.mark.parametrize("batch_dim", (1, 10))
def test_density_estimator_log_prob_shapes_with_embedding(
    density_estimator_build_fn,
    input_sample_dim,
    input_event_shape,
    condition_event_shape,
    batch_dim,
):
    """Test whether `loss` of DensityEstimators follow the shape convention."""
    density_estimator, inputs, conditions = _build_density_estimator_and_tensors(
        density_estimator_build_fn,
        input_event_shape,
        condition_event_shape,
        batch_dim,
        input_sample_dim,
    )

    losses = density_estimator.log_prob(inputs, condition=conditions)
    assert losses.shape == (input_sample_dim, batch_dim)


@pytest.mark.parametrize(
    "density_estimator_build_fn",
    (
        build_mdn,
        build_maf,
        build_maf_rqs,
        build_nsf,
        build_zuko_bpf,
        build_zuko_gf,
        build_zuko_maf,
        build_zuko_naf,
        build_zuko_ncsf,
        build_zuko_nice,
        build_zuko_nsf,
        build_zuko_sospf,
        build_zuko_unaf,
        build_categoricalmassestimator,
        build_mnle,
    ),
)
@pytest.mark.parametrize("sample_shape", ((), (1,), (2, 3)))
@pytest.mark.parametrize("input_event_shape", ((1,), (4,)))
@pytest.mark.parametrize("condition_event_shape", ((1,), (7,)))
@pytest.mark.parametrize("batch_dim", (1, 10))
def test_density_estimator_sample_shapes(
    density_estimator_build_fn,
    sample_shape,
    input_event_shape,
    condition_event_shape,
    batch_dim,
):
    """Test whether `loss` of DensityEstimators follow the shape convention."""
    density_estimator, _, conditions = _build_density_estimator_and_tensors(
        density_estimator_build_fn, input_event_shape, condition_event_shape, batch_dim
    )
    samples = density_estimator.sample(sample_shape, condition=conditions)
    if density_estimator_build_fn == build_categoricalmassestimator:
        # Our categorical is always 1D and does not return `input_event_shape`.
        input_event_shape = ()
    elif density_estimator_build_fn == build_mnle:
        input_event_shape = (input_event_shape[0] + 1,)
    assert samples.shape == (*sample_shape, batch_dim, *input_event_shape)


@pytest.mark.parametrize(
    "density_estimator_build_fn",
    (
        build_mdn,
        build_maf,
        build_maf_rqs,
        build_nsf,
        build_zuko_bpf,
        build_zuko_gf,
        build_zuko_maf,
        build_zuko_naf,
        build_zuko_ncsf,
        build_zuko_nice,
        build_zuko_nsf,
        build_zuko_sospf,
        build_zuko_unaf,
        build_categoricalmassestimator,
        build_mnle,
    ),
)
@pytest.mark.parametrize("input_event_shape", ((1,), (4,)))
@pytest.mark.parametrize("condition_event_shape", ((1,), (7,)))
@pytest.mark.parametrize("batch_dim", (1, 10))
def test_correctness_of_density_estimator_loss(
    density_estimator_build_fn,
    input_event_shape,
    condition_event_shape,
    batch_dim,
):
    """Test whether identical inputs lead to identical loss values."""
    input_sample_dim = 2
    density_estimator, inputs, condition = _build_density_estimator_and_tensors(
        density_estimator_build_fn,
        input_event_shape,
        condition_event_shape,
        batch_dim,
        input_sample_dim,
    )
    losses = density_estimator.loss(inputs, condition=condition)
    assert torch.allclose(losses[0, :], losses[1, :])


def _build_density_estimator_and_tensors(
    density_estimator_build_fn: str,
    input_event_shape: Tuple[int],
    condition_event_shape: Tuple[int],
    batch_dim: int,
    input_sample_dim: int = 1,
):
    """Helper function for all tests that deal with shapes of density estimators."""
    if density_estimator_build_fn == build_categoricalmassestimator:
        input_event_shape = (1,)
    elif density_estimator_build_fn == build_mnle:
        input_event_shape = (
            input_event_shape[0] + 1,
        )  # 1 does not make sense for mixed.

    # Use discrete thetas such that categorical density esitmators can also use them.
    building_thetas = torch.randint(
        0, 4, (1000, *input_event_shape), dtype=torch.float32
    )
    building_xs = torch.randn((1000, *condition_event_shape))
    if len(condition_event_shape) > 1:
        embedding_net = CNNEmbedding(condition_event_shape, kernel_size=1)
    else:
        embedding_net = torch.nn.Identity()

    if density_estimator_build_fn == build_mnle:
        building_thetas[:, :-1] += 5.0  # Make continuous dims positive for log-tf.
        density_estimator = density_estimator_build_fn(
            building_thetas, building_xs, embedding_net=embedding_net
        )
    elif density_estimator_build_fn == build_categoricalmassestimator:
        density_estimator = density_estimator_build_fn(
            building_thetas, building_xs, embedding_net=embedding_net
        )
    else:
        density_estimator = density_estimator_build_fn(
            torch.randn_like(building_thetas),
            torch.randn_like(building_xs),
            embedding_net=embedding_net,
        )

    inputs = building_thetas[:batch_dim]
    condition = building_xs[:batch_dim]

    inputs = inputs.unsqueeze(0)
    inputs = inputs.expand(input_sample_dim, -1, -1)
    condition = condition
    return density_estimator, inputs, condition
