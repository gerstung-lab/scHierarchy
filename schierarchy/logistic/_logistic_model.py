import logging
from datetime import date
from typing import List, Optional

import numpy as np
import pandas as pd
import scvi
from anndata import AnnData
from cell2location.models.base._pyro_mixin import (
    AutoGuideMixinModule,
    PltExportMixin,
    QuantileMixin,
    init_to_value,
)
from pyro import clear_param_store
from pyro.infer.autoguide import AutoNormalMessenger, init_to_feasible, init_to_mean
from scvi import _CONSTANTS
from scvi.data._anndata import _setup_anndata, _setup_extra_categorical_covs
from scvi.model.base import BaseModelClass, PyroSampleMixin, PyroSviTrainMixin
from scvi.module.base import PyroBaseModuleClass

from ._logistic_module import HierarchicalLogisticPyroModel

logger = logging.getLogger(__name__)


def infer_tree(labels_df, level_keys):
    """

    Parameters
    ----------
    labels_df
        DataFrame with annotations
    level_keys
        List of column names from top to bottom levels (from less detailed to more detailed)

    Returns
    -------
    List of edges between level len(n_levels - 1 )

    """
    # for multiple layers of hierarchy
    if len(level_keys) > 1:
        tree_inferred = [{} for i in range(len(level_keys) - 1)]
        for i in range(len(level_keys) - 1):
            layer_p = labels_df.loc[:, level_keys[i]]
            layer_ch = labels_df.loc[:, level_keys[i + 1]]
            for j in range(labels_df.shape[0]):
                if layer_p[j] not in tree_inferred[i].keys():
                    tree_inferred[i][layer_p[j]] = [layer_ch[j]]
                else:
                    if layer_ch[j] not in tree_inferred[i][layer_p[j]]:
                        tree_inferred[i][layer_p[j]].append(layer_ch[j])
    # if only one level
    else:
        tree_inferred = [list(labels_df[level_keys[0]].unique())]

    return tree_inferred


"""def _setup_summary_stats(adata, level_keys):
    n_cells = adata.shape[0]
    n_vars = adata.shape[1]
    n_cells_per_label_per_level = [
        adata.obs.groupby(group).size().values.astype(int) for group in level_keys
    ]

    n_levels = len(level_keys)

    summary_stats = {
        "n_cells": n_cells,
        "n_vars": n_vars,
        "n_levels": n_levels,
        "n_cells_per_label_per_level": n_cells_per_label_per_level,
    }

    adata.uns["_scvi"]["summary_stats"] = summary_stats
    adata.uns["tree"] = infer_tree(adata.obsm["_scvi_extra_categoricals"], level_keys)

    logger.info(
        "Successfully registered anndata object containing {} cells, {} vars, "
        "{} cell annotation levels.".format(n_cells, n_vars, n_levels)
    )
    return summary_stats"""


class LogisticBaseModule(PyroBaseModuleClass, AutoGuideMixinModule):
    def __init__(
        self,
        model,
        init_loc_fn=init_to_mean(fallback=init_to_feasible),
        guide_class=AutoNormalMessenger,
        guide_kwargs: Optional[dict] = None,
        **kwargs,
    ):
        """
        Module class which defines AutoGuide given model. Supports multiple model architectures.

        Parameters
        ----------
        amortised
            boolean, use a Neural Network to approximate posterior distribution of location-specific (local) parameters?
        encoder_mode
            Use single encoder for all variables ("single"), one encoder per variable ("multiple")
            or a single encoder in the first step and multiple encoders in the second step ("single-multiple").
        encoder_kwargs
            arguments for Neural Network construction (scvi.nn.FCLayers)
        kwargs
            arguments for specific model class - e.g. number of genes, values of the prior distribution
        """
        super().__init__()
        self.hist = []
        self._model = model(**kwargs)

        if guide_kwargs is None:
            guide_kwargs = dict()

        self._guide = guide_class(
            self.model,
            init_loc_fn=init_loc_fn,
            **guide_kwargs
            # create_plates=model.create_plates,
        )

        self._get_fn_args_from_batch = self._model._get_fn_args_from_batch

    @property
    def model(self):
        return self._model

    @property
    def guide(self):
        return self._guide

    @property
    def list_obs_plate_vars(self):
        return self.model.list_obs_plate_vars()

    def init_to_value(self, site):

        if getattr(self.model, "np_init_vals", None) is not None:
            init_vals = {
                k: getattr(self.model, f"init_val_{k}")
                for k in self.model.np_init_vals.keys()
            }
        else:
            init_vals = dict()
        return init_to_value(site=site, values=init_vals)


class LogisticModel(
    QuantileMixin, PyroSampleMixin, PyroSviTrainMixin, PltExportMixin, BaseModelClass
):
    """
    Model which estimates per cluster average mRNA count account for batch effects. User-end model class.

    https://github.com/BayraktarLab/cell2location

    Parameters
    ----------
    adata
        single-cell AnnData object that has been registered via :func:`~scvi.data.setup_anndata`.
    level_keys
        List of column names from top to bottom levels (from less detailed to more detailed)
    use_gpu
        Use the GPU?
    **model_kwargs
        Keyword args for :class:`~scvi.external.LocationModelLinearDependentWMultiExperimentModel`

    Examples
    --------
    TODO add example
    >>>
    """

    def __init__(
        self,
        adata: AnnData,
        level_keys: Optional[List[str]] = None,
        laplace_learning_mode: str = "fixed-sigma",
        # tree: list,
        model_class=None,
        **model_kwargs,
    ):
        # in case any other model was created before that shares the same parameter names.
        clear_param_store()

        super().__init__(adata)

        # register categorical levels
        cat_loc, cat_key = _setup_extra_categorical_covs(self.adata, level_keys)
        scvi.data.register_tensor_from_anndata(
            self.adata,
            registry_key=_CONSTANTS.CAT_COVS_KEY,
            adata_attr_name=cat_loc,
            adata_key_name=cat_key,
        )
        # add index for each cell (provided to pyro plate for correct minibatching)
        adata.obs["_indices"] = np.arange(adata.n_obs).astype("int64")
        # for minibatch learning, selected indices lay in "ind_x"
        scvi.data.register_tensor_from_anndata(
            adata,
            registry_key="ind_x",
            adata_attr_name="obs",
            adata_key_name="_indices",
        )

        self.n_cells_per_label_per_level_ = [
            self.adata.obs.groupby(group).size().values.astype(int)
            for group in level_keys
        ]
        self.n_levels_ = len(level_keys)
        self.level_keys_ = level_keys
        self.tree_ = infer_tree(self.adata.obsm["_scvi_extra_categoricals"], level_keys)
        self.laplace_learning_mode_ = laplace_learning_mode

        if model_class is None:
            model_class = HierarchicalLogisticPyroModel
        self.module = LogisticBaseModule(
            model=model_class,
            n_obs=self.summary_stats["n_cells"],
            n_vars=self.summary_stats["n_vars"],
            n_levels=self.n_levels_,
            n_cells_per_label_per_level=self.n_cells_per_label_per_level_,
            tree=self.tree_,
            laplace_learning_mode=self.laplace_learning_mode_,
            **model_kwargs,
        )
        self.samples = dict()
        self.init_params_ = self._get_init_params(locals())

    @staticmethod
    def setup_anndata(
        adata: AnnData,
        batch_key: Optional[str] = None,
        labels_key: Optional[str] = None,
        layer: Optional[str] = None,
        categorical_covariate_keys: Optional[List[str]] = None,
        continuous_covariate_keys: Optional[List[str]] = None,
        copy: bool = False,
    ) -> Optional[AnnData]:
        """
        %(summary)s.
        Parameters
        ----------
        %(param_adata)s
        %(param_batch_key)s
        %(param_labels_key)s
        %(param_layer)s
        %(param_cat_cov_keys)s
        %(param_cont_cov_keys)s
        %(param_copy)s
        Returns
        -------
        %(returns)s
        """
        return _setup_anndata(
            adata,
            batch_key=batch_key,
            labels_key=labels_key,
            layer=layer,
            categorical_covariate_keys=categorical_covariate_keys,
            continuous_covariate_keys=continuous_covariate_keys,
            copy=copy,
        )

    """@staticmethod
    def setup_anndata_v2(
        adata: AnnData,
        # layer: Optional[str] = None,
        # level_keys: Optional[List[str]] = None,
        batch_key: Optional[str] = None,
        labels_key: Optional[str] = None,
        copy: bool = False,
    ) -> Optional[AnnData]:

        if copy:
            adata = adata.copy()

        if adata.is_view:
            raise ValueError(
                "Please run `adata = adata.copy()` or use the copy option in this function."
            )

        adata.uns["_scvi"] = {}
        adata.uns["_scvi"]["scvi_version"] = scvi.__version__

        batch_key = _setup_batch(adata, batch_key)
        labels_key = _setup_labels(adata, labels_key)
        x_loc, x_key = _setup_x(adata, layer)

        data_registry = {
            _CONSTANTS.X_KEY: {"attr_name": x_loc, "attr_key": x_key},
            _CONSTANTS.BATCH_KEY: {"attr_name": "obs", "attr_key": batch_key},
            _CONSTANTS.LABELS_KEY: {"attr_name": "obs", "attr_key": labels_key},
        }

        cat_loc, cat_key = _setup_extra_categorical_covs(adata, level_keys)
        data_registry[_CONSTANTS.CAT_COVS_KEY] = {
            "attr_name": cat_loc,
            "attr_key": cat_key,
        }
        #####

        # add the data_registry to anndata
        _register_anndata(adata, data_registry_dict=data_registry)

        logger.debug("Registered keys:{}".format(list(data_registry.keys())))
        # _setup_summary_stats(adata, level_keys)

        logger.info("Please do not further modify adata until model is trained.")

        _verify_and_correct_data_format(adata, data_registry)

        if copy:
            return adata"""

    def _export2adata(self, samples):
        # add factor filter and samples of all parameters to unstructured data
        # TODO think what else to save
        results = {
            "model_name": str(self.module.__class__.__name__),
            "date": str(date.today()),
            "var_names": self.adata.var_names.tolist(),
            "obs_names": self.adata.obs_names.tolist(),
            # "tree": self.module.model.tree,
            "post_sample_means": samples["post_sample_means"],
            "post_sample_stds": samples["post_sample_stds"],
            "post_sample_q05": samples["post_sample_q05"],
            "post_sample_q95": samples["post_sample_q95"],
        }

        return results

    def export_posterior(
        self,
        adata,
        prediction: bool = False,
        sample_kwargs: Optional[dict] = None,
        export_slot: str = "mod",
        add_to_varm: list = ["means", "stds", "q05", "q95"],
    ):
        """
        Summarise posterior distribution and export results (cell abundance) to anndata object:
        1. adata.obsm: Estimated references expression signatures (average mRNA count in each cell type),
            as pd.DataFrames for each posterior distribution summary `add_to_varm`,
            posterior mean, sd, 5% and 95% quantiles (['means', 'stds', 'q05', 'q95']).
            If export to adata.varm fails with error, results are saved to adata.var instead.
        2. adata.uns: Posterior of all parameters, model name, date,
            cell type names ('factor_names'), obs and var names.

        Parameters
        ----------
        adata
            anndata object where results should be saved
        prediction
            Prediction mode predicts cell labels on new data.
        sample_kwargs
            arguments for self.sample_posterior (generating and summarising posterior samples), namely:
                num_samples - number of samples to use (Default = 1000).
                batch_size - data batch size (keep low enough to fit on GPU, default 2048).
                use_gpu - use gpu for generating samples?
        export_slot
            adata.uns slot where to export results
        add_to_varm
            posterior distribution summary to export in adata.varm (['means', 'stds', 'q05', 'q95']).
        Returns
        -------

        """

        sample_kwargs = sample_kwargs if isinstance(sample_kwargs, dict) else dict()

        label_keys = list(
            self.adata.uns["_scvi"]["extra_categoricals"]["mappings"].keys()
        )

        # when prediction mode change to evaluation mode and swap adata object
        if prediction:
            self.module.eval()
            self.module.model.prediction = True
            # use version of this function for prediction
            self.module._get_fn_args_from_batch = (
                self.module.model._get_fn_args_from_batch
            )
            # resize plates for according to the validation object
            self.module.model.n_obs = adata.n_obs
            # create index column
            adata.obs["_indices"] = np.arange(adata.n_obs).astype("int64")
            # for minibatch learning, selected indices lay in "ind_x"
            # scvi.data.register_tensor_from_anndata(
            #    adata,
            #    registry_key="ind_x",
            #    adata_attr_name="obs",
            #    adata_key_name="_indices",
            # )
            # if all columns with labels don't exist, create them and fill with 0s
            if np.all(~np.isin(label_keys, adata.obs.columns)):
                adata.obs.loc[:, label_keys] = 0
            # substitute adata object
            adata_train = self.adata.copy()
            self.adata = self._validate_anndata(adata)
            # self.adata = adata

            # generate samples from posterior distributions for all parameters
            # and compute mean, 5%/95% quantiles and standard deviation
            self.samples = self.sample_posterior(**sample_kwargs)

            # revert adata object substitution
            self.adata = adata_train
            self.module.train()
            self.module.model.prediction = False
            # re-set default version of this function
            self.module._get_fn_args_from_batch = (
                self.module.model._get_fn_args_from_batch
            )
            obs_names = adata.obs_names
        else:
            # generate samples from posterior distributions for all parameters
            # and compute mean, 5%/95% quantiles and standard deviation
            self.samples = self.sample_posterior(**sample_kwargs)
            obs_names = self.adata.obs_names

        # export posterior distribution summary for all parameters and
        # annotation (model, date, var, obs and cell type names) to anndata object
        adata.uns[export_slot] = self._export2adata(self.samples)

        # export estimated expression in each cluster
        # first convert np.arrays to pd.DataFrames with cell type and observation names
        # data frames contain mean, 5%/95% quantiles and standard deviation, denoted by a prefix
        for i in range(self.n_levels_):
            categories = self.adata.uns["_scvi"]["extra_categoricals"]["mappings"][
                label_keys[i]
            ]
            for k in add_to_varm:
                sample_df = pd.DataFrame(
                    self.samples[f"post_sample_{k}"].get(f"weight_level_{i}", None),
                    columns=[f"{k}_weight_{label_keys[i]}_{c}" for c in categories],
                    index=self.adata.var_names,
                )
                try:
                    adata.varm[f"{k}_weight_{label_keys[i]}"] = sample_df.loc[
                        adata.var_names, :
                    ]
                except ValueError:
                    # Catching weird error with obsm: `ValueError: value.index does not match parent’s axis 1 names`
                    adata.var[sample_df.columns] = sample_df.loc[adata.var_names, :]

                sample_df = pd.DataFrame(
                    self.samples[f"post_sample_{k}"].get(f"label_prob_{i}", None),
                    columns=obs_names,
                    index=[f"{k}_label_{label_keys[i]}_{c}" for c in categories],
                ).T
                try:
                    # TODO change to user input name
                    adata.obsm[f"{k}_label_prob_{label_keys[i]}"] = sample_df.loc[
                        adata.obs_names, :
                    ]
                except ValueError:
                    # Catching weird error with obsm: `ValueError: value.index does not match parent’s axis 1 names`
                    adata.obs[sample_df.columns] = sample_df.loc[adata.obs_names, :]

        return adata


# TODO plot QC - prediction accuracy curve
