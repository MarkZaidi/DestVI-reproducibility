import logging
from typing import OrderedDict, List
import numpy as np
import torch

from scvi import _CONSTANTS
from scvi._compat import Literal
from scvi.compose import (
    BaseModuleClass,
    FCLayers,
    LossRecorder,
    auto_move_data,
)
from scvi.distributions import NegativeBinomial
from torch.distributions import Normal


def identity(x):
    return x


class HSTDeconv(BaseModuleClass):
    """
    Model for hierarchical deconvolution of spatial transriptomics.

    Parameters
    ----------
    n_spots
        Number of input spots
    sc_params
        Tuple of ndarray of shapes [(n_genes, n_labels), (n_genes)] containing the dictionnary and log dispersion parameters
    prior_weight
        Whether to sample the minibatch by the number of total observations or the monibatch size
    """

    def __init__(
        self,
        n_spots: int,
        n_labels: int,
        sc_state_dict: List[OrderedDict],
        n_hidden: int,
        n_layers: int,
        n_latent: int,
        n_genes: int,
        mean_vprior=None,
        var_vprior=None,
        spatial_prior: bool = False,
        spatial_agg: str = None,
        lamb: float = None,
        training_mask: torch.Tensor = None,
        amortization: Literal["none", "latent", "proportion", "both"] = "none",
    ):
        super().__init__()
        self.n_spots = n_spots
        self.n_labels = n_labels
        self.n_hidden = n_hidden
        self.n_latent = n_latent
        self.n_genes = n_genes
        self.amortization = amortization

        # Spatial prior specific options
        self.spatial_prior = spatial_prior
        assert spatial_prior == (spatial_agg is not None)
        self.spatial_agg = spatial_agg
        self.lamb = lamb
        logging.info(lamb)
        if training_mask is None:
            logging.info("No training mask")
            self.training_mask = torch.ones(n_genes).bool()
            print(self.training_mask)
            n_ins = n_genes
        else:
            logging.info("With training mask")
            self.training_mask = training_mask
            n_ins = self.training_mask.sum().item()

        assert (not spatial_prior) == (self.lamb is None)
        # unpack and copy parameters
        self.decoder = FCLayers(
            n_in=n_latent,
            n_out=n_hidden,
            n_cat_list=[n_labels],
            n_layers=n_layers,
            n_hidden=n_hidden,
            dropout_rate=0,
            use_layer_norm=True,
            use_batch_norm=False,
        )
        self.px_decoder = torch.nn.Sequential(
            torch.nn.Linear(n_hidden, n_genes), torch.nn.Softplus()
        )
        # don't compute gradient for those parameters
        self.decoder.load_state_dict(sc_state_dict[0])
        for param in self.decoder.parameters():
            param.requires_grad = False
        self.px_decoder.load_state_dict(sc_state_dict[1])
        for param in self.px_decoder.parameters():
            param.requires_grad = False
        self.register_buffer("px_o", torch.tensor(sc_state_dict[2]))

        # cell_type specific factor loadings
        self.V = torch.nn.Parameter(torch.randn(self.n_labels + 1, self.n_spots))

        # within cell_type factor loadings
        self.gamma = torch.nn.Parameter(
            torch.randn(n_latent, self.n_labels, self.n_spots)
        )

        if mean_vprior is not None:
            print("USING VAMP PRIOR")
            self.p = mean_vprior.shape[1]
            self.register_buffer("mean_vprior", torch.tensor(mean_vprior))
            self.register_buffer("var_vprior", torch.tensor(var_vprior))
        else:
            self.mean_vprior = None
            self.var_vprior = None
        # noise from data
        self.eta = torch.nn.Parameter(torch.randn(self.n_genes, device="cuda"))
        # additive gene bias
        self.beta = torch.nn.Parameter(0.01 * torch.randn(self.n_genes, device="cuda"))

        # create additional neural nets for amortization
        # within cell_type factor loadings
        self.gamma_encoder = torch.nn.Sequential(
            FCLayers(
                n_in=n_ins,
                n_out=n_hidden,
                n_cat_list=[],
                n_layers=2,
                n_hidden=n_hidden,
                dropout_rate=0.1,
            ),
            torch.nn.Linear(n_hidden, n_latent * n_labels),
        )
        # cell type loadings
        self.V_encoder = FCLayers(
            n_in=n_ins,
            n_out=self.n_labels + 1,
            n_layers=2,
            n_hidden=n_hidden,
            dropout_rate=0.1,
        )

    @torch.no_grad()
    @auto_move_data
    def get_proportions(self, x=None, keep_noise=False) -> np.ndarray:
        """Returns the loadings."""
        if self.amortization in ["both", "proportion"]:
            # get estimated unadjusted proportions
            res = torch.nn.functional.softplus(self.V_encoder(x))
        else:
            res = (
                torch.nn.functional.softplus(self.V).cpu().numpy().T
            )  # n_spots, n_labels + 1
        # remove dummy cell type proportion values
        if not keep_noise:
            res = res[:, :-1]
        # normalize to obtain adjusted proportions
        res = res / res.sum(axis=1).reshape(-1, 1)
        return res

    @torch.no_grad()
    @auto_move_data
    def get_gamma(self, x=None) -> torch.Tensor:
        """
        Returns the loadings.

        Returns
        -------
        type
            tensor
        """
        # get estimated unadjusted proportions
        if self.amortization in ["latent", "both"]:
            gamma = self.gamma_encoder(x)
            return torch.transpose(gamma, 0, 1).reshape(
                (self.n_latent, self.n_labels, -1)
            )  # n_latent, n_labels, minibatch
        else:
            return self.gamma.cpu().numpy()  # (n_latent, n_labels, n_spots)

    @auto_move_data
    def get_ct_specific_expression(self, x=None, ind_x=None, y=None):
        """
        Returns cell type specific gene expression at the queried spots.

        Parameters
        ----------
        x
            data
        ind_x
            indices
        y
            cell types
        """
        # cell-type specific gene expression, shape (minibatch, celltype, gene).
        beta = torch.nn.functional.softplus(self.beta)  # n_genes

        # obtain the relevant gammas
        if self.amortization in ["both", "latent"]:
            x_ = torch.log(1 + x)
            gamma_ind = torch.transpose(self.gamma_encoder(x_), 0, 1).reshape(
                (self.n_latent, self.n_labels, -1)
            )
        else:
            gamma_ind = self.gamma[
                :, :, ind_x[:, 0]
            ]  # n_latent, n_labels, minibatch_size

        # calculate cell type specific expression
        gamma_select = gamma_ind[
            :, y[:, 0], torch.arange(ind_x.shape[0])
        ].T  # minibatch_size, n_latent
        h = self.decoder(gamma_select, y)
        px_scale = self.px_decoder(h)  # (minibatch, n_genes)
        px_ct = torch.exp(self.px_o).unsqueeze(0) * beta.unsqueeze(0) * px_scale
        return px_ct  # shape (minibatch, genes)

    def _get_inference_input(self, tensors):
        # we perform MAP here, so we just need to subsample the variables
        return {}

    def _get_generative_input(self, tensors, inference_outputs):
        # print(tensors)
        x = tensors[_CONSTANTS.X_KEY]
        ind_x = tensors["ind_x"].long()
        x_n = tensors["x_n"]
        ind_n = tensors["ind_n"].long()

        input_dict = dict(x=x, ind_x=ind_x, x_n=x_n, ind_n=ind_n)
        return input_dict

    @auto_move_data
    def inference(self):
        return {}

    @auto_move_data
    def generative(self, x, ind_x, x_n, ind_n):
        """Build the deconvolution model for every cell in the minibatch."""
        M = x.shape[0]
        library = torch.sum(x, dim=1, keepdim=True)
        # setup all non-linearities
        beta = torch.nn.functional.softplus(self.beta)  # n_genes
        eps = torch.nn.functional.softplus(self.eta)  # n_genes
        x_ = torch.log(1 + x)
        # subsample parameters

        if self.amortization in ["both", "latent"]:
            gamma_ind = torch.transpose(
                self.gamma_encoder(x_[:, self.training_mask]), 0, 1
            ).reshape((self.n_latent, self.n_labels, -1))
        else:
            gamma_ind = self.gamma[
                :, :, ind_x[:, 0]
            ]  # n_latent, n_labels, minibatch_size

        if self.amortization in ["both", "proportion"]:
            v_ind = self.V_encoder(x_[:, self.training_mask])
        else:
            v_ind = self.V[:, ind_x[:, 0]].T  # minibatch_size, labels + 1
        v_ind = torch.nn.functional.softplus(v_ind)

        if self.spatial_prior:
            with torch.no_grad():
                if self.amortization in ["both", "proportion"]:
                    v_ind_n = self.V_encoder(x_n[..., self.training_mask])
                else:
                    v_ind_n = self.V[
                        :, ind_n
                    ].T  # n_neighbors, minibatch_size, labels + 1
                    v_ind_n = v_ind_n.transpose(0, 1)
                v_ind_n = torch.nn.functional.softplus(v_ind_n)
                # print(sel f.amortization, v_ind_n.shape)
        else:
            v_ind_n = None
        # reshape and get gene expression value for all minibatch
        gamma_ind = torch.transpose(
            gamma_ind, 2, 0
        )  # minibatch_size, n_labels, n_latent
        gamma_reshape = gamma_ind.reshape(
            (-1, self.n_latent)
        )  # minibatch_size * n_labels, n_latent
        enum_label = (
            torch.arange(0, self.n_labels).repeat((M)).view((-1, 1))
        )  # minibatch_size * n_labels, 1
        h = self.decoder(gamma_reshape, enum_label.cuda())
        px_rate = self.px_decoder(h).reshape(
            (M, self.n_labels, -1)
        )  # (minibatch, n_labels, n_genes)

        # add the dummy cell type
        eps = eps.repeat((M, 1)).view(
            M, 1, -1
        )  # (M, 1, n_genes) <- this is the dummy cell type

        # account for gene specific bias and add noise
        r_hat = torch.cat(
            [beta.unsqueeze(0).unsqueeze(1) * px_rate, eps], dim=1
        )  # M, n_labels + 1, n_genes
        # now combine them for convolution
        px_scale = torch.sum(v_ind.unsqueeze(2) * r_hat, dim=1)  # batch_size, n_genes
        px_rate = library * px_scale

        return dict(
            px_o=self.px_o,
            px_rate=px_rate,
            px_scale=px_scale,
            gamma=gamma_ind,
            v=v_ind,
            v_n=v_ind_n,
        )


    def get_loss_components(
        self,
        tensors,
        inference_outputs,
        generative_outputs,
        kl_weight: float = 1.0,
        n_obs: int = 1.0,
        loss_mask: torch.Tensor = None,
    ):
        if loss_mask is None:
            loss_mask = torch.ones(self.n_genes).bool()
        x = tensors[_CONSTANTS.X_KEY]
        px_rate = generative_outputs["px_rate"]
        px_o = generative_outputs["px_o"]
        gamma = generative_outputs["gamma"]

        reconst_loss_all = -NegativeBinomial(px_rate, logits=px_o).log_prob(x)[..., loss_mask]
        reconst_loss = reconst_loss_all.sum(-1)

        if self.spatial_prior:
            v = generative_outputs["v"]  # Shape minibatch_size, labels + 1
            v_n = generative_outputs[
                "v_n"
            ]  # Shape: minibatch_size. n_neighbors, labels + 1
            if self.spatial_agg == "pair":
                v = v.unsqueeze(1)
                reg = self.lamb * (v - v_n) ** 2
                # print(reg.shape)
                assert reg.ndim == 3
                reg = reg.sum(-1).sum(-1).mean()
            elif self.spatial_agg == "pairl1":
                v = v.unsqueeze(1)
                reg = self.lamb * torch.abs(v - v_n)
                assert reg.ndim == 3, (reg.shape, v.shape, v_n.shape)
                reg = reg.sum(-1).mean()
            elif self.spatial_agg == "mean":
                v_n = v_n.mean(1)
                reg = self.lamb * (v - v_n) ** 2
                assert reg.ndim == 2, (reg.shape, v.shape, v_n.shape)
                reg = reg.sum(-1).mean()
        else:
            reg = torch.tensor(0.0)

        # eta prior likelihood
        mean = torch.zeros_like(self.eta)
        scale = torch.ones_like(self.eta)
        glo_neg_log_likelihood_prior = -Normal(mean, scale).log_prob(self.eta).sum()
        # TODO: make this an option?
        glo_neg_log_likelihood_prior += torch.var(self.beta)

        # gamma prir likelihood
        if self.mean_vprior is None:
            # isotropic normal prior
            mean = torch.zeros_like(gamma)
            scale = torch.ones_like(gamma)
            neg_log_likelihood_prior = (
                -Normal(mean, scale).log_prob(gamma).sum(2).sum(1)
            )
        else:
            # vampprior
            # gamma is of shape n_latent, n_labels, minibatch_size
            gamma = gamma.unsqueeze(1)  # minibatch_size, 1, n_labels, n_latent
            mean_vprior = torch.transpose(self.mean_vprior, 0, 1).unsqueeze(
                0
            )  # 1, p, n_labels, n_latent
            var_vprior = torch.transpose(self.var_vprior, 0, 1).unsqueeze(
                0
            )  # 1, p, n_labels, n_latent
            pre_lse = (
                Normal(mean_vprior, torch.sqrt(var_vprior)).log_prob(gamma).sum(-1)
            )  # minibatch, p, n_labels
            log_likelihood_prior = torch.logsumexp(pre_lse, 1) - np.log(
                self.p
            )  # minibatch, n_labels
            neg_log_likelihood_prior = -log_likelihood_prior.sum(1)  # minibatch
            # mean_vprior is of shape n_labels, p, n_latent

        loss = (
            n_obs * torch.mean(reconst_loss + kl_weight * neg_log_likelihood_prior)
            + glo_neg_log_likelihood_prior
        )
        loss += reg
        return dict(
            loss=loss,
            reconst_loss=reconst_loss,
            reg=reg,
            neg_log_likelihood_prior=neg_log_likelihood_prior,
            glo_neg_log_likelihood_prior=glo_neg_log_likelihood_prior,
            reconst_loss_all=reconst_loss_all,
        )

    def loss(
        self,
        tensors,
        inference_outputs,
        generative_outputs,
        kl_weight: float = 1.0,
        n_obs: int = 1.0,
        loss_mask: torch.Tensor = None,
    ):
        loss_dict = self.get_loss_components(
            tensors=tensors,
            inference_outputs=inference_outputs,
            generative_outputs=generative_outputs,
            kl_weight=kl_weight,
            n_obs=n_obs,
            loss_mask=loss_mask,
        )
        loss = loss_dict["loss"]
        reconst_loss =loss_dict["reconst_loss"]
        reg = loss_dict["reg"]
        glo_neg_log_likelihood_prior = loss_dict["glo_neg_log_likelihood_prior"]
        reconst_loss_all = loss_dict["reconst_loss_all"]
        loss_rec = LossRecorder(
            # loss, reconst_loss, neg_log_likelihood_prior, glo_neg_log_likelihood_prior
            loss, reconst_loss, reg, glo_neg_log_likelihood_prior
        )
        loss_rec.reconstruction_loss_all = reconst_loss_all

        return loss_rec

    @torch.no_grad()
    def sample(
        self,
        tensors,
        n_samples=1,
        library_size=1,
    ):
        raise NotImplementedError("No sampling method for Stereoscope")
