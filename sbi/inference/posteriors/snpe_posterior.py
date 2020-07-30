# This file is part of sbi, a toolkit for simulation-based inference. sbi is licensed
# under the Affero General Public License v3, see <https://www.gnu.org/licenses/>.

from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
    TypeVar,
    Union,
    cast,
)

import torch
from torch import Tensor, log, nn

from sbi import utils as utils
from sbi.inference.posteriors.posterior import NeuralPosterior
from sbi.types import Shape
from sbi.utils import del_entries
from sbi.utils.torchutils import atleast_2d_float32_tensor, batched_first_of_batch


class SnpePosterior(NeuralPosterior):
    r"""Posterior $p(\theta|x)$ with `log_prob()` and `sample()` methods.<br/><br/>
    All inference methods in sbi train a neural network which is then used to obtain
    the posterior distribution. The `NeuralPosterior` class wraps the trained network
    such that one can directly evaluate the log probability and draw samples from the
    posterior. The neural network itself can be accessed via the `.net` attribute.
    <br/><br/>
    Specifically, this class offers the following functionality:<br/>
    - Correction of leakage (applicable only to SNPE): If the prior is bounded, the
      posterior resulting from SNPE can generate samples that lie outside of the prior
      support (i.e. the posterior leaks). This class rejects these samples or,
      alternatively, allows to sample from the posterior with MCMC. It also corrects the
      calculation of the log probability such that it compensates for the leakage.<br/>
    - Posterior inference from likelihood (SNL) and likelihood ratio (SRE): SNL and SRE
      learn to approximate the likelihood and likelihood ratio, which in turn can be
      used to generate samples from the posterior. This class provides the needed MCMC
      methods to sample from the posterior and to evaluate the log probability.

    """

    def __init__(
        self,
        method_family: str,
        neural_net: nn.Module,
        prior,
        x_shape: torch.Size,
        sample_with_mcmc: bool = True,
        mcmc_method: str = "slice_np",
        mcmc_parameters: Optional[Dict[str, Any]] = None,
        get_potential_function: Optional[Callable] = None,
    ):
        """
        Args:
            method_family: One of snpe, snl, snre_a or snre_b.
            neural_net: A classifier for SNRE, a density estimator for SNPE and SNL.
            prior: Prior distribution with `.log_prob()` and `.sample()`.
            x_shape: Shape of a single simulator output.
            sample_with_mcmc: Whether to sample with MCMC. Will always be `True` for SRE
                and SNL, but can also be set to `True` for SNPE if MCMC is preferred to
                deal with leakage over rejection sampling.
            mcmc_method: Method used for MCMC sampling, one of `slice_np`, `slice`,
                `hmc`, `nuts`. Currently defaults to `slice_np` for a custom numpy
                implementation of slice sampling; select `hmc`, `nuts` or `slice` for
                Pyro-based sampling.
            mcmc_parameters: Dictionary overriding the default parameters for MCMC.
                The following parameters are supported: `thin` to set the thinning
                factor for the chain, `warmup_steps` to set the initial number of
                samples to discard, `num_chains` for the number of chains, `init_strategy`
                for the initialisation strategy for chains; `prior` will draw init
                locations from prior, whereas `sir` will use Sequential-Importance-
                Resampling using `init_strategy_num_candidates` to find init
                locations.
            get_potential_function: Callable that returns the potential function used
                for MCMC sampling.
        """

        kwargs = del_entries(
            locals(), entries=("self", "__class__", "sample_with_mcmc")
        )
        super().__init__(**kwargs)

        self.set_sample_with_mcmc(sample_with_mcmc)

    @property
    def sample_with_mcmc(self) -> bool:
        """
        Return `True` if NeuralPosterior instance should use MCMC in `.sample()`.
        """
        return self._sample_with_mcmc

    @sample_with_mcmc.setter
    def sample_with_mcmc(self, value: bool) -> None:
        """See `set_sample_with_mcmc`."""
        self.set_sample_with_mcmc(value)

    def set_sample_with_mcmc(self, use_mcmc: bool) -> "NeuralPosterior":
        """Turns MCMC sampling on or off and returns `NeuralPosterior`.

        Args:
            use_mcmc: Flag to set whether or not MCMC sampling is used.

        Returns:
            `NeuralPosterior` for chainable calls.

        Raises:
            `ValueError` on attempt to turn off MCMC sampling for family of methods
            that do not support rejection sampling.
        """
        if not use_mcmc:
            if self._method_family not in ["snpe"]:
                raise ValueError(f"{self._method_family} cannot use MCMC for sampling.")
        self._sample_with_mcmc = use_mcmc
        return self

    def _log_prob_snpe(self, theta: Tensor, x: Tensor, norm_posterior: bool) -> Tensor:
        r"""
        Return posterior log probability $p(\theta|x)$.

        The posterior probability will be only normalized if explicitly requested,
        but it will be always zeroed out (i.e. given -∞ log-prob) outside the prior
        support.
        """

        unnorm_log_prob = self.net.log_prob(theta, x)

        # Force probability to be zero outside prior support.
        is_prior_finite = torch.isfinite(self._prior.log_prob(theta))

        masked_log_prob = torch.where(
            is_prior_finite,
            unnorm_log_prob,
            torch.tensor(float("-inf"), dtype=torch.float32),
        )

        log_factor = (
            log(self.leakage_correction(x=batched_first_of_batch(x)))
            if norm_posterior
            else 0
        )

        return masked_log_prob - log_factor

    @torch.no_grad()
    def leakage_correction(
        self,
        x: Tensor,
        num_rejection_samples: int = 10_000,
        force_update: bool = False,
        show_progress_bars: bool = False,
    ) -> Tensor:
        r"""Return leakage correction factor for a leaky posterior density estimate.

        The factor is estimated from the acceptance probability during rejection
        sampling from the posterior.

        NOTE: This is to avoid re-estimating the acceptance probability from scratch
              whenever `log_prob` is called and `norm_posterior_snpe=True`. Here, it
              is estimated only once for `self.default_x` and saved for later. We
              re-evaluate only whenever a new `x` is passed.

        Arguments:
            x: Conditioning context for posterior $p(\theta|x)$.
            num_rejection_samples: Number of samples used to estimate correction factor.
            force_update: Whether to force a reevaluation of the leakage correction even
                if the context `x` is the same as `self.default_x`. This is useful to
                enforce a new leakage estimate for rounds after the first (2, 3,..).
            show_progress_bars: Whether to show a progress bar during sampling.

        Returns:
            Saved or newly-estimated correction factor (as a scalar `Tensor`).
        """

        def acceptance_at(x: Tensor) -> Tensor:
            return utils.sample_posterior_within_prior(
                self.net, self._prior, x, num_rejection_samples, show_progress_bars
            )[1]

        # Check if the provided x matches the default x (short-circuit on identity).
        is_new_x = self.default_x is None or (
            x is not self.default_x and (x != self.default_x).any()
        )

        not_saved_at_default_x = self._leakage_density_correction_factor is None

        if is_new_x:  # Calculate at x; don't save.
            return acceptance_at(x)
        elif not_saved_at_default_x or force_update:  # Calculate at default_x; save.
            self._leakage_density_correction_factor = acceptance_at(self.default_x)

        return self._leakage_density_correction_factor  # type:ignore

    def sample(
        self,
        sample_shape: Shape = torch.Size(),
        x: Optional[Tensor] = None,
        show_progress_bars: bool = True,
        sample_with_mcmc: Optional[bool] = None,
        mcmc_method: Optional[str] = None,
        mcmc_parameters: Optional[Dict[str, Any]] = None,
    ) -> Tensor:
        r"""
        Return samples from posterior distribution $p(\theta|x)$.

        Samples are obtained either with rejection sampling or MCMC. SNPE can use
        rejection sampling and MCMC (which can help to deal with strong leakage). SNL
        and SRE are restricted to sampling with MCMC.

        Args:
            sample_shape: Desired shape of samples that are drawn from posterior. If
                sample_shape is multidimensional we simply draw `sample_shape.numel()`
                samples and then reshape into the desired shape.
            x: Conditioning context for posterior $p(\theta|x)$. If not provided,
                fall back onto `x_o` if previously provided for multiround training, or
                to a set default (see `set_default_x()` method).
            show_progress_bars: Whether to show sampling progress monitor.
            sample_with_mcmc: Optional parameter to override `self.sample_with_mcmc`.
            mcmc_method: Optional parameter to override `self.mcmc_method`.
            mcmc_parameters: Dictionary overriding the default parameters for MCMC.
                The following parameters are supported: `thin` to set the thinning
                factor for the chain, `warmup_steps` to set the initial number of
                samples to discard, `num_chains` for the number of chains, `init_strategy`
                for the initialisation strategy for chains; `prior` will draw init
                locations from prior, whereas `sir` will use Sequential-Importance-
                Resampling using `init_strategy_num_candidates` to find init
                locations.

        Returns:
            Samples from posterior.
        """

        x = atleast_2d_float32_tensor(self._x_else_default_x(x))
        self._ensure_single_x(x)
        self._ensure_x_consistent_with_default_x(x)
        num_samples = torch.Size(sample_shape).numel()

        sample_with_mcmc = (
            sample_with_mcmc if sample_with_mcmc is not None else self.sample_with_mcmc
        )
        mcmc_method = mcmc_method if mcmc_method is not None else self.mcmc_method
        mcmc_parameters = (
            mcmc_parameters if mcmc_parameters is not None else self.mcmc_parameters
        )

        if sample_with_mcmc:
            samples = self._sample_posterior_mcmc(
                x=x,
                num_samples=num_samples,
                show_progress_bars=show_progress_bars,
                mcmc_method=mcmc_method,
                **mcmc_parameters,
            )
        else:
            # Rejection sampling.
            samples, _ = utils.sample_posterior_within_prior(
                self.net,
                self._prior,
                x,
                num_samples=num_samples,
                show_progress_bars=show_progress_bars,
            )

        return samples.reshape((*sample_shape, -1))