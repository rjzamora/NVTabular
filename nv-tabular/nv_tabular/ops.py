import warnings
import os
import numpy as np
import cudf
from nv_tabular.dl_encoder import DLLabelEncoder
from nv_tabular.ds_writer import DatasetWriter


CONT = "continuous"
CAT = "categorical"
ALL = "all"


class Operator:
    columns = None

    def __init__(self, columns=columns):
        self.columns = columns

    @property
    def _id(self):
        return self.__class__.__name__

    def describe(self):
        raise NotImplementedError("All operators must have a desription.")

    def get_columns(self, cols_ctx, cols_grp, target_cols):
        # providing any operator with direct list of columns overwrites cols dict
        # burden on user to ensure columns exist in dataset (as discussed)
        if self.columns:
            return self.columns
        tar_cols = []
        for tar in target_cols:
            if tar in cols_ctx[cols_grp].keys():
                tar_cols = tar_cols + cols_ctx[cols_grp][tar]
        return tar_cols

    def export_op(self):
        export = {}
        export[self._id] = self.__dict__
        return export


class TransformOperator(Operator):
    preprocessing = False
    replace = False
    default_in = None
    default_out = None

    def __init__(self, columns=None, preprocessing=True, replace=False):
        super().__init__(columns=columns)
        self.preprocessing = preprocessing
        self.replace = replace

    def get_default_in(self):
        if self.default_in is None:
            raise NotImplementedError(
                "default_in columns have not been specified for this operator"
            )
        return self.default_in

    def get_default_out(self):
        if self.default_out is None:
            raise NotImplementedError(
                "default_out columns have not been specified for this operator"
            )
        return self.default_out
    
    
    def update_columns_ctx(self, columns_ctx, input_cols, new_cols, pro=False):

        """
        columns_ctx: columns context, belonging to the container workflow object
        input_cols: input columns; columns actioned on origin columns context key
        new_cols: new columns; new columns generated by operator to be added to columns context
        ----
        This function generalizes the action of updating the columns context dictionary
        of the container workflow object, after an operator has created new columns via a 
        new transformation of a subset or entire dataset.
        """
        new_key = self._id
        if not pro:
            input_cols = self.default_out
        columns_ctx[input_cols][new_key] = []
        columns_ctx[input_cols][new_key] = list(new_cols)
        if not self.preprocessing:
            columns_ctx["final"]["ctx"][input_cols].append(self._id)

    def apply_op(
        self, gdf: cudf.DataFrame, columns_ctx: dict, input_cols, target_cols="base", stats_context=None
    ):
        target_cols = self.get_columns(columns_ctx, input_cols, target_cols)
        new_gdf = self.op_logic(gdf, target_cols, stats_context=stats_context)
        self.update_columns_ctx(columns_ctx, input_cols, new_gdf.columns)
        return self.assemble_new_df(gdf, new_gdf, target_cols)
        
    
    def assemble_new_df(self, origin_gdf, new_gdf, target_columns):
        if not new_gdf:
            return origin_gdf
        if self.replace:
            origin_gdf[target_columns] = new_gdf
            # might need to change column names here too
            return origin_gdf
        return cudf.concat([origin_gdf, new_gdf], axis=1)

        
    def op_logic(self, gdf, target_columns, stats_context=None):
        raise NotImplementedError("""Must implement transform in the op_logic method,
                                     The return value must be a dataframe with all required
                                     transforms.""")
        
        
class DFOperator(TransformOperator):
    
    def required_stats(self):
        raise NotImplementedError(
            "Should consist of a list of identifiers, that should map to available statistics"
        )


class StatOperator(Operator):
    def read_itr(
        self, gdf: cudf.DataFrame, columns_ctx: dict, input_cols, target_cols="base"
    ):
        raise NotImplementedError(
            """The operation to conduct on the dataframe to observe the desired statistics."""
        )

    def read_fin(self):
        raise NotImplementedError(
            """Upon finalization of the statistics on all data frame chunks, 
                this function allows for final transformations on the statistics recorded.
                Can be 'pass' if unneeded."""
        )

    def registered_stats(self):
        raise NotImplementedError(
            """Should return a list of statistics this operator will collect.
                The list is comprised of simple string values."""
        )

    def stats_collected(self):
        raise NotImplementedError(
            """Should return a list of tuples of name and statistics operator."""
        )

    def clear(self):
        raise NotImplementedError(
            """zero and reinitialize all relevant statistical properties"""
        )


class MinMax(StatOperator):
    batch_mins = {}
    batch_maxs = {}
    mins = {}
    maxs = {}

    def apply_op(
        self, gdf: cudf.DataFrame, columns_ctx: dict, input_cols, target_cols="base"
    ):
        """ Iteration level Min Max collection, a chunk at a time
        """
        cols = self.get_columns(columns_ctx, input_cols, target_cols)
        for col in cols:
            col_min = min(gdf[col].dropna())
            col_max = max(gdf[col].dropna())
            if not col in self.batch_mins:
                self.batch_mins[col] = []
                self.batch_maxs[col] = []
            self.batch_mins[col].append(col_min)
            self.batch_maxs[col].append(col_max)
        return

    def read_fin(self):

        for col in self.batch_mins.keys():
            # required for exporting values later,
            # must move values from gpu if cupy->numpy not supported
            self.batch_mins[col] = cudf.Series(self.batch_mins[col]).tolist()
            self.batch_maxs[col] = cudf.Series(self.batch_maxs[col]).tolist()
            self.mins[col] = min(self.batch_mins[col])
            self.maxs[col] = max(self.batch_maxs[col])
        return

    def registered_stats(self):
        return ["mins", "maxs", "batch_mins", "batch_maxs"]

    def stats_collected(self):
        result = [
            ("mins", self.mins),
            ("maxs", self.maxs),
            ("batch_mins", self.batch_mins),
            ("batch_maxs", self.batch_maxs),
        ]
        return result

    def clear(self):
        self.batch_mins = {}
        self.batch_maxs = {}
        self.mins = {}
        self.maxs = {}
        return


class Moments(StatOperator):
    counts = {}
    means = {}
    varis = {}
    stds = {}

    def apply_op(
        self, gdf: cudf.DataFrame, columns_ctx: dict, input_cols, target_cols="base"
    ):
        """ Iteration-level moment algorithm (mean/std).
        """
        cols = self.get_columns(columns_ctx, input_cols, target_cols)
        for col in cols:
            if col not in self.counts:
                self.counts[col] = 0.0
                self.means[col] = 0.0
                self.varis[col] = 0.0
                self.stds[col] = 0.0

            # TODO: Harden this routine to handle 0-division.
            #       This algo may also break/overflow at scale.

            n1 = self.counts[col]
            n2 = float(len(gdf))

            v1 = self.varis[col]
            v2 = gdf[col].var()

            m1 = self.means[col]
            m2 = gdf[col].mean()

            self.counts[col] += n2
            self.means[col] = (m1 * n1 + m2 * n2) / self.counts[col]

            #  Variance
            t1 = n1 * v1
            t2 = n2 * v2
            t3 = n1 * ((m1 - self.means[col]) ** 2)
            t4 = n2 * ((m2 - self.means[col]) ** 2)
            t5 = n1 + n2
            self.varis[col] = (t1 + t2 + t3 + t4) / t5
        return

    def read_fin(self):
        """ Finalize statistical-moments algoprithm.
        """
        for col in self.varis.keys():
            self.stds[col] = float(np.sqrt(self.varis[col]))

    def registered_stats(self):
        return ["means", "stds", "vars", "counts"]

    def stats_collected(self):
        result = [
            ("means", self.means),
            ("stds", self.stds),
            ("vars", self.varis),
            ("counts", self.counts),
        ]
        return result

    def clear(self):
        self.counts = {}
        self.means = {}
        self.varis = {}
        self.stds = {}
        return


class Median(StatOperator):
    batch_medians = {}
    medians = {}

    def __init__(self, columns=None, fill=None):
        self.fill = fill

    def apply_op(
        self, gdf: cudf.DataFrame, columns_ctx: dict, input_cols, target_cols="base"
    ):
        """ Iteration-level median algorithm.
        """
        cols = self.get_columns(columns_ctx, input_cols, target_cols)
        for name in cols:
            if name not in self.batch_medians:
                self.batch_medians[name] = []
            col = gdf[name].copy()
            col = col.dropna().reset_index(drop=True).sort_values()
            if self.fill:
                self.batch_medians[name].append(self.fill)
            elif len(col) > 1:
                self.batch_medians[name].append(float(col[len(col) // 2]))
            else:
                self.batch_medians[name].append(0.0)
        return

    def read_fin(self, *args):
        """ Finalize median algorithm.
        """
        for col, val in self.batch_medians.items():
            self.batch_medians[col].sort()
            self.medians[col] = float(
                self.batch_medians[col][len(self.batch_medians[col]) // 2]
            )
        return

    def registered_stats(self):
        return ["medians"]

    def stats_collected(self):
        result = [("medians", self.medians)]
        return result

    def clear(self):
        self.batch_medians = {}
        self.medians = {}
        return


class Encoder(StatOperator):
    encoders = {}
    categories = {}

    def apply_op(
        self, gdf: cudf.DataFrame, columns_ctx: dict, input_cols, target_cols="base"
    ):
        """ Iteration-level categorical encoder update.
        """
        cols = self.get_columns(columns_ctx, input_cols, target_cols)
        if not cols:
            return
        for name in cols:
            if not name in self.encoders:
                self.encoders[name] = DLLabelEncoder(name)
                gdf[name].append([None])
            self.encoders[name].fit(gdf[name])
        return

    def read_fin(self, *args):
        """ Finalize categorical encoders (get categories).
        """
        for name, val in self.encoders.items():
            self.categories[name] = self.cat_read_all_files(val)
        return

    def cat_read_all_files(self, cat_obj):
        cat_size = cat_obj._cats.shape[0]
        return cat_size + cat_obj.cat_exp_count

    def registered_stats(self):
        return ["encoders", "categories"]

    def stats_collected(self):
        result = [("encoders", self.encoders), ("categories", self.categories)]
        return result

    def clear(self):
        self.encoders = {}
        self.categories = {}
        return


class Export(TransformOperator):
    default_in = ALL
    default_out = ALL

    def __init__(
        self,
        path="./ds_export",
        nfiles=1,
        shuffle=True,
        columns=None,
        preprocessing=False,
        replace=False,
    ):
        super().__init__(columns=columns, preprocessing=preprocessing, replace=replace)
        self.path = path
        if not os.path.exists(path):
            os.makedirs(path)
        self.nfiles = nfiles
        self.shuffle = True

    def op_logic(self, gdf: cudf.DataFrame, target_columns: list, stats_context=None):
        gdf.to_parquet(self.path)
        return 


class ZeroFill(TransformOperator):
    default_in = CONT
    default_out = CONT

    def op_logic(self, gdf: cudf.DataFrame, target_columns: list, stats_context=None):
        cont_names = target_columns
        if not cont_names:
            return gdf
        z_gdf = gdf[cont_names].fillna(0)
        z_gdf.columns = [f"{col}_{self._id}" for col in z_gdf.columns]
        z_gdf = z_gdf * (z_gdf >= 0).astype("int")
        return z_gdf


class LogOp(TransformOperator):
    default_in = CONT
    default_out = CONT

    def op_logic(self, gdf: cudf.DataFrame, target_columns: list, stats_context=None):
        cont_names = target_columns
        if not cont_names:
            return gdf
        new_gdf = np.log(gdf[cont_names].astype(np.float32) + 1)
        new_cols = [f"{col}_{self._id}" for col in new_gdf.columns]
        new_gdf.columns = new_cols
        return new_gdf


class Normalize(DFOperator):
    """ Normalize the continuous variables.
    """

    default_in = CONT
    default_out = CONT

    @property
    def req_stats(self):
        return [Moments()]

    def op_logic(self, gdf: cudf.DataFrame, target_columns: list, stats_context=None):
        cont_names = target_columns
        if not cont_names or not stats_context["stds"]:
            return 
        gdf = self.apply_mean_std(gdf, stats_context, cont_names)
        return gdf

    def apply_mean_std(self, gdf, stats_context, cont_names):
        new_gdf = cudf.DataFrame()
        for name in cont_names:
            if stats_context["stds"][name] > 0:
                new_col = f"{name}_{self._id}"
                new_gdf[new_col] = (gdf[name] - stats_context["means"][name]) / (
                    stats_context["stds"][name]
                )
                new_gdf[new_col] = new_gdf[new_col].astype("float32")
        return new_gdf


class FillMissing(DFOperator):
    MEDIAN = "median"
    CONSTANT = "constant"
    default_in = CONT
    default_out = CONT

    def __init__(
        self,
        fill_strategy=MEDIAN,
        fill_val=0,
        filler={},
        add_col=False,
        columns=None,
        preprocessing=True,
        replace=False,
        default_in=None,
        default_out=None,
    ):
        super().__init__(columns=columns, preprocessing=preprocessing, replace=replace)
        self.fill_strategy = fill_strategy
        self.fill_val = fill_val
        self.add_col = add_col
        self.filler = filler

    @property
    def req_stats(self):
        return [Median()]

    def op_logic(self, gdf: cudf.DataFrame, target_columns: list, stats_context=None):
        cont_names = target_columns
        if not cont_names or not stats_context["medians"]:
            return gdf
        z_gdf = self.apply_filler(gdf[cont_names], stats_context, cont_names)
        return z_gdf

    def apply_filler(self, gdf, stats_context, cont_names):
        na_names = [name for name in cont_names if gdf[name].isna().sum()]
        if self.add_col:
            gdf = self.add_na_indicators(gdf, na_names, cont_names)
        for col in na_names:
            gdf[col] = gdf[col].fillna(np.float32(stats_context["medians"][col]))
        gdf.columns = [f"{name}_{self._id}" for name in gdf.columns]
        return gdf

    def add_na_indicators(self, gdf: cudf.DataFrame, na_names, cat_names):
        gdf = cudf.DataFrame()
        for name in na_names:
            name_na = name + "_na"
            gdf[name_na] = gdf[name].isna()
            if name_na not in cat_names:
                cat_names.append(name_na)
        return gdf


class Categorify(DFOperator):
    """ Transform the categorical variables to that type.
    """

    embed_sz = {}
    cat_names = []
    default_in = CAT
    default_out = CAT

    @property
    def req_stats(self):
        return [Encoder()]

    def op_logic(self, gdf: cudf.DataFrame, target_columns: list, stats_context=None):
        cat_names = target_columns
        new_gdf = cudf.DataFrame()
        if not cat_names:
            return gdf
        cat_names = [name for name in cat_names if name in gdf.columns]
        new_cols = []
        for name in cat_names:
            new_col = f"{name}_{self._id}"
            new_cols.append(new_col)
            new_gdf[new_col] = stats_context["encoders"][name].transform(gdf[name])
            new_gdf[new_col] = new_gdf[new_col].astype("int64")
        return new_gdf

    def get_emb_sz(self, encoders, cat_names):
        work_in = {}
        for key in encoders.keys():
            work_in[key] = encoders[key] + 1
        ret_list = [(n, self.def_emb_sz(work_in, n)) for n in sorted(cat_names)]
        return ret_list

    def emb_sz_rule(self, n_cat: int) -> int:
        return min(16, round(1.6 * n_cat ** 0.56))

    def def_emb_sz(self, classes, n, sz_dict=None):
        """Pick an embedding size for `n` depending on `classes` if not given in `sz_dict`.
        """
        sz_dict = sz_dict if sz_dict else {}
        n_cat = classes[n]
        sz = sz_dict.get(n, int(self.emb_sz_rule(n_cat)))  # rule of thumb
        self.embed_sz[n] = sz
        return n_cat, sz


all_ops = {
    MinMax()._id: MinMax,
    Moments()._id: Moments,
    Median()._id: Median,
    Encoder()._id: Encoder,
    Export()._id: Export,
    ZeroFill()._id: ZeroFill,
    LogOp()._id: LogOp,
    Normalize()._id: Normalize,
    FillMissing()._id: FillMissing,
    Categorify()._id: Categorify,
}
