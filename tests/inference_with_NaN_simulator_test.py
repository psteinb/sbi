# This file is part of sbi, a toolkit for simulation-based inference. sbi is licensed
# under the Apache License Version 2.0, see <https://www.apache.org/licenses/>

import pytest
import torch
from torch import eye, ones, zeros
from torch.distributions import MultivariateNormal

from sbi import utils as utils
from sbi.diagnostics import run_sbc
from sbi.inference import (
    NPE_A,
    NPE_C,
    SNL,
    SRE,
    DirectPosterior,
    simulate_for_sbi,
)
from sbi.simulators.linear_gaussian import (
    linear_gaussian,
    samples_true_posterior_linear_gaussian_uniform_prior,
)
from sbi.utils import RestrictionEstimator
from sbi.utils.metrics import check_c2st
from sbi.utils.sbiutils import handle_invalid_x
from sbi.utils.user_input_checks import (
    check_sbi_inputs,
    process_prior,
    process_simulator,
)


@pytest.mark.parametrize(
    "x_shape",
    (
        torch.Size((10, 1)),
        torch.Size((10, 10)),
    ),
)
def test_handle_invalid_x(x_shape):
    x = torch.rand(x_shape)
    x[x < 0.1] = float("nan")
    x[x > 0.9] = float("inf")
    x[-1, :] = 1.0  # make sure there is one row of valid entries.

    x_is_valid, *_ = handle_invalid_x(x, exclude_invalid_x=True)

    assert torch.isfinite(x[x_is_valid]).all()


@pytest.mark.parametrize("snpe_method", [NPE_A, NPE_C])
def test_z_scoring_warning(snpe_method: type):
    # Create data with large variance.
    num_dim = 2
    theta = torch.ones(100, num_dim)
    x = torch.rand(100, num_dim)
    x[:50] += 1e7

    # Make sure a warning is raised because z-scoring will map these data to duplicate
    # data points.
    with pytest.warns(UserWarning, match="Z-scoring these simulation outputs"):
        snpe_method(utils.BoxUniform(zeros(num_dim), ones(num_dim))).append_simulations(
            theta, x
        ).train(max_num_epochs=1)


@pytest.mark.slow
@pytest.mark.parametrize(
    ("method", "percent_nans"),
    (
        (NPE_C, 0.05),
        pytest.param(SNL, 0.05, marks=pytest.mark.xfail),
        pytest.param(SRE, 0.05, marks=pytest.mark.xfail),
    ),
)
def test_inference_with_nan_simulator(method: type, percent_nans: float):
    # likelihood_mean will be likelihood_shift+theta
    num_dim = 3
    likelihood_shift = -1.0 * ones(num_dim)
    likelihood_cov = 0.3 * eye(num_dim)
    x_o = zeros(1, num_dim)
    num_samples = 500
    num_simulations = 5000

    def linear_gaussian_nan(
        theta, likelihood_shift=likelihood_shift, likelihood_cov=likelihood_cov
    ):
        x = linear_gaussian(theta, likelihood_shift, likelihood_cov)
        # Set nan randomly.
        x[torch.rand(x.shape) < (percent_nans * 1.0 / x.shape[1])] = float("nan")

        return x

    prior = utils.BoxUniform(-2.0 * ones(num_dim), 2.0 * ones(num_dim))
    target_samples = samples_true_posterior_linear_gaussian_uniform_prior(
        x_o,
        likelihood_shift=likelihood_shift,
        likelihood_cov=likelihood_cov,
        num_samples=num_samples,
        prior=prior,
    )

    simulator = process_simulator(linear_gaussian_nan, prior, False)
    check_sbi_inputs(simulator, prior)
    inference = method(prior=prior)

    theta, x = simulate_for_sbi(simulator, prior, num_simulations)
    _ = inference.append_simulations(theta, x).train()
    posterior = inference.build_posterior()

    samples = posterior.sample((num_samples,), x=x_o)

    # Compute the c2st and assert it is near chance level of 0.5.
    check_c2st(samples, target_samples, alg=f"{method}")

    # run sbc
    num_sbc_samples = 100
    thetas = prior.sample((num_sbc_samples,))
    xs = simulator(thetas)
    ranks, daps = run_sbc(thetas, xs, posterior, num_posterior_samples=1000)
    assert torch.isfinite(ranks).all()


@pytest.mark.slow
def test_inference_with_restriction_estimator():
    # likelihood_mean will be likelihood_shift+theta
    num_dim = 3
    likelihood_shift = -1.0 * ones(num_dim)
    likelihood_cov = 0.3 * eye(num_dim)
    x_o = zeros(1, num_dim)
    num_samples = 1000
    num_simulations = 1000

    def linear_gaussian_nan(
        theta, likelihood_shift=likelihood_shift, likelihood_cov=likelihood_cov
    ):
        condition = theta[:, 0] < 0.0
        x = linear_gaussian(theta, likelihood_shift, likelihood_cov)
        x[condition] = float("nan")

        return x

    prior = utils.BoxUniform(-2.0 * ones(num_dim), 2.0 * ones(num_dim))
    target_samples = samples_true_posterior_linear_gaussian_uniform_prior(
        x_o,
        likelihood_shift=likelihood_shift,
        likelihood_cov=likelihood_cov,
        num_samples=num_samples,
        prior=prior,
    )

    simulator = process_simulator(linear_gaussian_nan, prior, False)
    check_sbi_inputs(simulator, prior)
    restriction_estimator = RestrictionEstimator(prior=prior)
    proposal = prior
    num_rounds = 2

    for r in range(num_rounds):
        theta, x = simulate_for_sbi(simulator, proposal, num_simulations)
        restriction_estimator.append_simulations(theta, x)
        if r < num_rounds - 1:
            _ = restriction_estimator.train()
        proposal = restriction_estimator.restrict_prior()

    all_theta, all_x, _ = restriction_estimator.get_simulations()

    # test passing restricted prior to inference and using process_prior, see #790.
    restricted_prior = restriction_estimator.restrict_prior()
    prior = process_prior(restricted_prior)[0]

    # Any method can be used in combination with the `RejectionEstimator`.
    inference = NPE_C(prior=prior)
    posterior_estimator = inference.append_simulations(all_theta, all_x).train()

    # Build posterior.
    posterior = DirectPosterior(
        prior=prior,
        posterior_estimator=posterior_estimator,
    ).set_default_x(x_o)

    samples = posterior.sample((num_samples,))

    # Compute the c2st and assert it is near chance level of 0.5.
    check_c2st(samples, target_samples, alg=f"{NPE_C}")


@pytest.mark.parametrize("prior", ("uniform", "gaussian"))
def test_restricted_prior_log_prob(prior):
    """Test whether the log-prob method of the restricted prior works appropriately."""

    def simulator(theta):
        perturbed_theta = theta + 0.5 * torch.randn(2)
        perturbed_theta[theta[:, 0] < 0.8] = torch.as_tensor([
            float("nan"),
            float("nan"),
        ])
        return perturbed_theta

    if prior == "uniform":
        prior = utils.BoxUniform(-2 * torch.ones(2), 2 * torch.ones(2))
    else:
        prior = MultivariateNormal(torch.zeros(2), torch.eye(2))

    prior, _, prior_returns_numpy = process_prior(prior)
    simulator = process_simulator(simulator, prior, prior_returns_numpy)
    theta, x = simulate_for_sbi(simulator, prior, 1000)

    restriction_estimator = RestrictionEstimator(prior=prior)
    restriction_estimator.append_simulations(theta, x)
    _ = restriction_estimator.train(max_num_epochs=40)
    restricted_prior = restriction_estimator.restrict_prior()

    # test restricted prior log_prob
    restricted_prior, _, _ = process_prior(restricted_prior)

    def integrate_grid(distribution):
        resolution = 500
        range_ = 4
        x = torch.linspace(-range_, range_, resolution)
        y = torch.linspace(-range_, range_, resolution)
        X, Y = torch.meshgrid(x, y, indexing="ij")
        xy = torch.stack([X, Y])
        xy = torch.reshape(xy, (2, resolution**2)).T
        dist_on_grid = torch.exp(distribution.log_prob(xy))
        integral = torch.sum(dist_on_grid) / resolution**2 * (2 * range_) ** 2
        return integral

    integal_restricted = integrate_grid(restricted_prior)
    error = torch.abs(integal_restricted - torch.as_tensor(1.0))
    assert error < 0.015, "The restricted prior does not integrate to one."

    theta = prior.sample((10_000,))
    restricted_prior_probs = torch.exp(restricted_prior.log_prob(theta))

    valid_thetas = restricted_prior._accept_reject_fn(theta).bool()
    assert torch.all(restricted_prior_probs[valid_thetas] > 0.0), (
        "Accepted theta have zero probability."
    )
    assert torch.all(restricted_prior_probs[torch.logical_not(valid_thetas)] == 0.0), (
        "Rejected theta has non-zero probablity."
    )
