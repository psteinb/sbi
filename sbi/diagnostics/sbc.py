import warnings
from typing import Callable, Optional, Union

import torch
from joblib import Parallel, delayed
from torch import Tensor, ones, zeros
from torch.distributions import Distribution, Normal, Uniform
from tqdm.auto import tqdm

from sbi.inference import simulate_for_sbi
from sbi.inference.posteriors.base_posterior import NeuralPosterior
from sbi.simulators.simutils import tqdm_joblib
from sbi.utils.metrics import c2st


def sbc_in_batches(
    prior: Distribution,
    simulator: Callable,
    posterior: Union[NeuralPosterior, Distribution],
    num_sbc_samples: int = 1000,
    num_posterior_samples: int = 100,
    sbc_batch_size: int = 1,
    num_workers: int = 1,
    ranking_rv: Optional[Union[str, Distribution]] = "gaussian",
    show_progress_bars: bool = True,
):
    """Run SBC in batches and parallelized across `num_workers`."""

    if ranking_rv == "gaussian":
        ranking_rv = Normal(loc=0, scale=10)
    assert isinstance(
        ranking_rv, Distribution
    ), "Ranking RV must be a torch Distribution."

    if num_sbc_samples < 1000:
        warnings.warn(
            """Number of SBC samples should be on the order ~100 to give realiable
            results. We recommend using 1000."""
        )
    if num_posterior_samples < 100:
        warnings.warn(
            """Number of posterior samples for ranking should be on the order
            of ~100 to give reliable SBC results. We recommend using at least 100."""
        )

    thos, xos = simulate_for_sbi(
        simulator,
        prior,
        num_sbc_samples,
        simulation_batch_size=1000,
    )

    thos_batches = torch.split(thos, sbc_batch_size, dim=0)
    xos_batches = torch.split(xos, sbc_batch_size, dim=0)

    if num_workers > 1:
        # Parallelize the sequence of batches across workers.
        # We use the solution proposed here: https://stackoverflow.com/a/61689175
        # to update the pbar only after the workers finished a task.
        with tqdm_joblib(
            tqdm(
                thos_batches,
                disable=not show_progress_bars,
                desc=f"""Running {num_sbc_samples} sbc runs in {len(thos_batches)}
                    batches.""",
                total=len(thos_batches),
            )
        ) as progress_bar:
            sbc_outputs = Parallel(n_jobs=num_workers)(
                delayed(sbc_on_batch)(
                    thos_batch, xos_batch, posterior, num_posterior_samples, ranking_rv
                )
                for thos_batch, xos_batch in zip(thos_batches, xos_batches)
            )
    else:
        pbar = tqdm(
            total=num_sbc_samples,
            disable=not show_progress_bars,
            desc=f"Running {num_sbc_samples} sbc samples.",
        )

        with pbar:
            sbc_outputs = []
            for thos_batch, xos_batch in zip(thos_batches, xos_batches):
                sbc_outputs.append(
                    sbc_on_batch(
                        thos_batch,
                        xos_batch,
                        posterior,
                        num_posterior_samples,
                        ranking_rv,
                    )
                )
                pbar.update(sbc_batch_size)

    # Aggregate results.
    ranks = []
    dap_samples = []
    log_probs = []
    for out in sbc_outputs:
        ranks.append(out[0])
        log_probs.append(out[1])
        dap_samples.append(out[2])

    ranks = torch.cat(ranks)
    dap_samples = torch.cat(dap_samples)
    log_probs = torch.cat(log_probs)

    return ranks, log_probs, dap_samples


def sbc_on_batch(thos, xos, posterior, L, ranking_rv):
    """Return SBC results for a batch of SBC parameters and data from prior.

    Parameters
        thos: samples of true theta, assumed to have shape (nsamples, ndims)
        xos: samples of true x, assumed to have shape (nsamples, ndims)
        posterior: approximated posterior p(theta|x)
        L: number of draws from the posterior given x_o
        ranking_rv: random variable to perform SBC ranking under

    Returns
        ranks: ranks of true parameters vs. posterior samples under the specified RV,
            for each posterior dimension.
        log_prob_thos: log prob of true parameters under the approximate posterior.
        dap_samples: samples from the data averaged posterior for the current batch,
            i.e., a single sample from each approximate posterior.
    """

    log_prob_thos = torch.zeros(thos.shape[0])
    dap_samples = torch.zeros_like(thos)
    ranks = torch.zeros_like(thos)

    for idx, (theta_o, x_o) in enumerate(zip(thos, xos)):
        # Log prob of true params under posterior.
        log_prob_thos[idx] = posterior.log_prob(theta_o, x=x_o)

        # Rank true params vs posterior params under ranking_rv.
        ths = posterior.sample((L,), x=x_o, show_progress_bars=False)
        dap_samples[idx] = ths[
            0,
        ]
        # rank for each posterior dimension.
        for dim in range(thos.shape[1]):
            ranks[idx, dim] = (
                (ranking_rv.log_prob(ths[:, dim]) < ranking_rv.log_prob(theta_o[dim]))
                .sum()
                .item()
            )

    return ranks, log_prob_thos, dap_samples


def sbc_checks(
    ranks: Tensor,
    log_probs: Tensor,
    prior_samples: Tensor,
    dap_samples: Tensor,
    num_ranks: int,
):
    """Return uniformity checks, data averaged posterior checks and NLTP for SBC."""
    if ranks.shape[0] < 100:
        warnings.warn(
            """You are computing SBC checks with less than 100 samples. These checks
            should be based on a large number of test samples theta_o, x_o. We
            recommend using at least 100."""
        )

    ks_pvals, c2st_ranks = check_uniformity(ranks, num_ranks)
    c2st_scrores_dap = check_prior_vs_dap(prior_samples, dap_samples)
    nltp = torch.mean(-log_probs)

    return dict(
        ks_pvals=ks_pvals,
        c2st_ranks=c2st_ranks,
        c2st_dap=c2st_scrores_dap,
        nltp=nltp,
    )


def check_prior_vs_dap(prior_samples: Tensor, dap_samples: Tensor):
    """Return the c2st accuracy between prior and data avaraged posterior samples.

    c2st is calculated for each dimension separately.
    """

    assert prior_samples.shape == dap_samples.shape

    return torch.tensor(
        [
            c2st(s1.unsqueeze(1), s2.unsqueeze(1))
            for s1, s2 in zip(prior_samples.T, dap_samples.T)
        ]
    )


def check_uniformity(ranks, num_ranks, num_repetitions: int = 1):
    """Return p-values and c2st scores for uniformity of the ranks.

    Calculates Kolomogorov-Smirnov test using scipy.
    """

    from scipy.stats import kstest, uniform

    kstest_pvals = torch.tensor(
        [kstest(rks, uniform(loc=0, scale=num_ranks).cdf)[1] for rks in ranks.T],
        dtype=torch.float32,
    )

    c2st_scores = torch.tensor(
        [
            [
                c2st(
                    rks.unsqueeze(1),
                    Uniform(zeros(1), num_ranks * ones(1)).sample((ranks.shape[0],)),
                )
                for rks in ranks.T
            ]
            for _ in range(num_repetitions)
        ]
    )

    if (c2st_scores.std(0) > 0.05).any():
        warnings.warn(
            "C2ST score variability is larger {0.05}, result may be unreliable."
        )

    return kstest_pvals, c2st_scores.mean(0)
