import warnings
import os
import numpy as np
import cudf
from ds_itr.dl_encoder import DLLabelEncoder



def get_columns(cols_ctx, cols_grp, target_cols):
    tar_cols = []
    for tar in target_cols:
        if tar in cols_ctx[cols_grp].keys():
            tar_cols = tar_cols + cols_ctx[cols_grp][tar]
    return tar_cols
    

class Operator:
    @property
    def _id(self):
        return self.__class__.__name__

    def describe(self):
        raise NotImplementedError("All operators must have a desription.")
        
    
    def apply_op(
        self,
        gdf: cudf.DataFrame,
        columns_ctx: dict,
        input_cols, 
        target_cols='base'
    ):
        raise NotImplementedError(
            """The operation to be applied on the data frame chunk, given the required statistics.
                """
        )

class TransformOperator(Operator):

    def cols_suffix(self):
        return f"_{self._id}"


class DFOperator(TransformOperator):

    def required_stats(self):
        raise NotImplementedError(
            "Should consist of a list of identifiers, that should map to available statistics"
        )

class StatOperator(Operator):

    def read_itr(
        self,
        gdf: cudf.DataFrame,
        columns_ctx: dict,
        input_cols, 
        target_cols='base'
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
        self, gdf: cudf.DataFrame, columns_ctx: dict, input_cols, target_cols='base'
    ):
        """ Iteration level Min Max collection, a chunk at a time
        """
        cols = get_columns(columns_ctx, input_cols, target_cols)
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
        self, gdf: cudf.DataFrame, columns_ctx: dict, input_cols, target_cols='base'
    ):
        """ Iteration-level moment algorithm (mean/std).
        """
        cols = get_columns(columns_ctx, input_cols, target_cols)
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

    def __init__(self, fill=None):
        self.fill = fill

    def apply_op(
        self, gdf: cudf.DataFrame, columns_ctx: dict, input_cols, target_cols='base'
    ):
        """ Iteration-level median algorithm.
        """
        cols = get_columns(columns_ctx, input_cols, target_cols)
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
        self, gdf: cudf.DataFrame, columns_ctx: dict, input_cols, target_cols='base'
    ):
        """ Iteration-level categorical encoder update.
        """
        cols = get_columns(columns_ctx, input_cols, target_cols)
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
        file_paths = (
            [
                f"{cat_obj.col}/{x}"
                for x in os.listdir(cat_obj.col)
                if x.endswith("parquet")
            ]
            if os.path.exists(cat_obj.col)
            else []
        )
        for fi in file_paths:
            chunk = cudf.read_parquet(fi)
            cat_size = cat_size + chunk.shape[0]
        return cat_size

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
    def __init__(self, path='./ds_export', nfiles=1, shuffle=True, **kwargs):
        self.path = path
        self.nfiles = nfiles
        self.shuffle = True

    def apply_op(
        self, gdf: cudf.DataFrame, columns_ctx: dict, input_cols, target_cols='base'
    ):
        writer = DatasetWriter(self.path, nfiles=self.nfiles)
        writer.write(gdf, shuffle=self.shuffle)
        writer.write_metadata()
        return gdf


class ZeroFill(TransformOperator):
    def apply_op(
        self, gdf: cudf.DataFrame, columns_ctx: dict, input_cols, target_cols='base'
    ):
        import pdb; pdb.set_trace()
        new_key = self._id
        columns_ctx[input_cols][new_key] = []
        
        cont_names = get_columns(columns_ctx, input_cols, target_cols)
        if not cont_names:
            return gdf
        z_gdf = gdf[cont_names].fillna(0)
        z_gdf.columns = [f"{col}_{self._id}" for col in z_gdf.columns]
        z_gdf = z_gdf * (z_gdf>=0).astype("int")
        columns_ctx[input_cols][new_key] = list(z_gdf.columns)
        return cudf.concat([gdf, z_gdf], axis=1)


class LogOp(TransformOperator):
    def apply_op(
        self, gdf: cudf.DataFrame, columns_ctx: dict, input_cols, target_cols='base'
    ):
        new_key = self._id
        columns_ctx[input_cols][new_key] = []
        cont_names = get_columns(columns_ctx, input_cols, target_cols)
        if not cont_names:
            return gdf
        new_gdf = np.log(gdf[cont_names].astype(np.float32) + 1)
        new_cols = [f"{col}_{self._id}"for col in new_gdf.columns]
        new_gdf.columns = new_cols
        columns_ctx[input_cols][new_key] = list(new_cols)
        gdf = cudf.concat([gdf, new_gdf], axis=1)
        return gdf


class Normalize(DFOperator):
    """ Normalize the continuous variables.
    """

    @property
    def req_stats(self):
        return [Moments()]

    def apply_op(
        self, gdf: cudf.DataFrame, columns_ctx: dict, input_cols, target_cols='base'
    ):
        cont_names = get_columns(columns_ctx, input_cols, target_cols)
        new_key = self._id
        columns_ctx[input_cols][new_key] = []
        if not cont_names or not stats_context["stds"]:
            return gdf
        gdf, new_cols = self.apply_mean_std(gdf, stats_context, cont_names)
        columns_ctx[input_cols][new_key] = list(new_cols)
        return gdf

    def apply_mean_std(self, gdf, stats_context, cont_names):
        new_cols = []
        for name in cont_names:
            if stats_context["stds"][name] > 0:
                new_col = f"{name}_{self._id}"
                gdf[new_col] = (gdf[name] - stats_context["means"][name]) / (
                    stats_context["stds"][name]
                )
            gdf[new_col] = gdf[new_col].astype("float32")
            new_cols.append(new_col)
        return gdf, new_cols


class FillMissing(DFOperator):
    MEDIAN = "median"
    CONSTANT = "constant"

    def __init__(self, fill_strategy=MEDIAN, fill_val=0, add_col=False):
        self.fill_strategy = fill_strategy
        self.fill_val = fill_val
        self.add_col = add_col
        self.filler = {}

    @property
    def req_stats(self):
        return [Median()]

    def apply_op(
        self, gdf: cudf.DataFrame, columns_ctx: dict, input_cols, target_cols='base'
    ):
        new_key = self._id
        columns_ctx[input_cols][new_key] = []
        cont_names = get_columns(columns_ctx, input_cols, target_cols)
        if not cont_names or not stats_context["medians"]:
            return gdf
        z_gdf = self.apply_filler(gdf, stats_context, cont_names)
        columns_ctx[input_cols][new_key] = list(z_gdf.columns)
        return cudf.concat([gdf, z_gdf], axis=1)

    def apply_filler(self, gdf, stats_context, cont_names):
        na_names = [name for name in cont_names if gdf[name].isna().sum()]
        if self.add_col:
            gdf = self.add_na_indicators(gdf, na_names, cont_names)
        for col in na_names:
            z_gdf = gdf[col].fillna(np.float32(stats_context["medians"][col]))
        z_gdf.columns = [f"{name}_{self._id}" for name in z_gdf.columns]
        return z_gdf

    def add_na_indicators(self, gdf: cudf.DataFrame, na_names, cat_names):
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

    @property
    def req_stats(self):
        return [Encoder()]

    def apply_op(
        self, gdf: cudf.DataFrame, columns_ctx: dict, input_cols, target_cols='base'
    ):
        new_key = self._id
        columns_ctx[input_cols][new_key] = []
        cat_names = get_columns(columns_ctx, input_cols, target_cols)
        if not cat_names:
            return gdf
        cat_names = [name for name in cat_names if name in gdf.columns]
        new_cols = []
        for name in cat_names:
            new_col = f"{name}_{self._id}"
            new_cols.append(new_col)
            gdf[new_col] = stats_context["encoders"][name].transform(gdf[name])
            gdf[new_col] = gdf[new_col].astype("int64")
        columns_ctx[input_cols][new_key] = list(new_cols)
        return gdf

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
    MinMax()._id: MinMax(),
    Moments()._id: Moments(), 
    Median()._id: Median(), 
    Encoder()._id: Encoder(), 
    Export()._id: Export(), 
    ZeroFill()._id: ZeroFill(),
    LogOp()._id: LogOp(),
    Normalize()._id: Normalize(),
    FillMissing()._id: FillMissing(),
    Categorify()._id: Categorify()
}