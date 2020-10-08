#
# Copyright (c) 2020, NVIDIA CORPORATION.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import functools
import logging
import os
import warnings
from io import BytesIO
from uuid import uuid4

import cudf
import dask_cudf
from cudf.io.parquet import ParquetWriter as pwriter
from dask.dataframe.io.parquet.utils import _analyze_paths
from dask.utils import natural_sort_key
from pyarrow import parquet as pq

from .dataset_engine import DatasetEngine
from .shuffle import Shuffle, _shuffle_gdf
from .writer import ThreadedWriter

LOG = logging.getLogger("nvtabular")


class ParquetDatasetEngine(DatasetEngine):
    """ParquetDatasetEngine is a Dask-based version of cudf.read_parquet."""

    def __init__(
        self,
        paths,
        part_size,
        storage_options,
        row_groups_per_part=None,
        legacy=False,
        batch_size=None,
    ):
        # TODO: Improve dask_cudf.read_parquet performance so that
        # this class can be slimmed down.
        super().__init__(paths, part_size, storage_options)
        self.batch_size = batch_size
        self._metadata, self._base = self.metadata
        self._pieces = None
        if row_groups_per_part is None:
            file_path = self._metadata.row_group(0).column(0).file_path
            path0 = (
                self.fs.sep.join([self._base, file_path])
                if file_path != ""
                else self._base  # This is a single file
            )

        if row_groups_per_part is None:
            rg_byte_size_0 = (
                cudf.io.read_parquet(path0, row_groups=0, row_group=0)
                .memory_usage(deep=True, index=True)
                .sum()
            )
            row_groups_per_part = self.part_size / rg_byte_size_0
            if row_groups_per_part < 1.0:
                warnings.warn(
                    f"Row group size {rg_byte_size_0} is bigger than requested part_size "
                    f"{self.part_size}"
                )
                row_groups_per_part = 1.0

        self.row_groups_per_part = int(row_groups_per_part)

        assert self.row_groups_per_part > 0

    @property
    @functools.lru_cache(1)
    def metadata(self):
        paths = self.paths
        fs = self.fs
        if len(paths) > 1:
            # This is a list of files
            dataset = pq.ParquetDataset(paths, filesystem=fs, validate_schema=False)
            base, fns = _analyze_paths(paths, fs)
        elif fs.isdir(paths[0]):
            # This is a directory
            dataset = pq.ParquetDataset(paths[0], filesystem=fs, validate_schema=False)
            allpaths = fs.glob(paths[0] + fs.sep + "*")
            base, fns = _analyze_paths(allpaths, fs)
        else:
            # This is a single file
            dataset = pq.ParquetDataset(paths[0], filesystem=fs)
            base = paths[0]
            fns = [None]

        metadata = None
        if dataset.metadata:
            # We have a metadata file
            return dataset.metadata, base
        else:
            # Collect proper metadata manually
            metadata = None
            for piece, fn in zip(dataset.pieces, fns):
                md = piece.get_metadata()
                if fn:
                    md.set_file_path(fn)
                if metadata:
                    metadata.append_row_groups(md)
                else:
                    metadata = md
            return metadata, base

    @property
    def num_rows(self):
        metadata, _ = self.metadata
        return metadata.num_rows

    def to_ddf(self, columns=None):
        return dask_cudf.read_parquet(
            self.paths,
            columns=columns,
            gather_statistics=False,
            split_row_groups=self.row_groups_per_part,
        )


class ParquetWriter(ThreadedWriter):
    def __init__(self, out_dir, **kwargs):
        super().__init__(out_dir, **kwargs)
        self.data_paths = []
        self.data_writers = []
        self.data_bios = []
        for i in range(self.num_out_files):
            if self.use_guid:
                fn = f"{i}.{guid()}.parquet"
            else:
                fn = f"{i}.parquet"

            path = os.path.join(out_dir, fn)
            self.data_paths.append(path)
            if self.bytes_io:
                bio = BytesIO()
                self.data_bios.append(bio)
                self.data_writers.append(pwriter(bio, compression=None))
            else:
                self.data_writers.append(pwriter(path, compression=None))

    def _write_table(self, idx, data):
        self.data_writers[idx].write_table(data)

    def _write_thread(self):
        while True:
            item = self.queue.get()
            try:
                if item is self._eod:
                    break
                idx, data = item
                with self.write_locks[idx]:
                    self._write_table(idx, data)
            finally:
                self.queue.task_done()

    @classmethod
    def write_special_metadata(cls, md, fs, out_dir):
        # Sort metadata by file name and convert list of
        # tuples to a list of metadata byte-blobs
        md_list = [m[1] for m in sorted(list(md.items()), key=lambda x: natural_sort_key(x[0]))]

        # Aggregate metadata and write _metadata file
        _write_pq_metadata_file(md_list, fs, out_dir)

    def _close_writers(self):
        md_dict = {}
        for writer, path in zip(self.data_writers, self.data_paths):
            fn = path.split(self.fs.sep)[-1]
            md_dict[fn] = writer.close(metadata_file_path=fn)
        return md_dict

    def _bytesio_to_disk(self):
        for bio, path in zip(self.data_bios, self.data_paths):
            gdf = cudf.io.read_parquet(bio, index=False)
            bio.close()
            if self.shuffle == Shuffle.PER_WORKER:
                gdf = _shuffle_gdf(gdf)
            gdf.to_parquet(path, compression=None, index=False)
        return


def _write_pq_metadata_file(md_list, fs, path):
    """ Converts list of parquet metadata objects into a single shared _metadata file. """
    if md_list:
        metadata_path = fs.sep.join([path, "_metadata"])
        _meta = cudf.io.merge_parquet_filemetadata(md_list) if len(md_list) > 1 else md_list[0]
        with fs.open(metadata_path, "wb") as fil:
            _meta.tofile(fil)
    return


def guid():
    """Simple utility function to get random hex string"""
    return uuid4().hex
