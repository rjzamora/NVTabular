import cudf
import sys
import numba
import cupy as cp
import os
import random
import numpy as np
import pyarrow.parquet as pq
import threading
import time

#
# Helper Function definitions
#


def _allowable_batch_size(gpu_memory_frac, row_size):
    free_mem, _ = numba.cuda.current_context().get_memory_info()
    gpu_memory = free_mem * gpu_memory_frac
    return max(int(gpu_memory / row_size), 1)


def _get_read_engine(engine, file_path, **kwargs):
    if engine is None:
        engine = file_path.split(".")[-1]
    if not isinstance(engine, str):
        raise TypeError("Expecting engine as string type.")

    if engine == "csv":
        return CSVFileReader(file_path, **kwargs)
    elif engine == "parquet":
        return PQFileReader(file_path, **kwargs)
    else:
        raise ValueError("Unrecognized read engine.")


#
# GPUFileReader Base Class
#


class GPUFileReader:
    def __init__(self, file_path, gpu_memory_frac, batch_size, row_size=None, **kwargs):
        """ GPUFileReader Constructor
        """
        self.file = None
        self.file_path = file_path
        self.row_size = row_size
        self.intialize_reader(gpu_memory_frac, batch_size, **kwargs)

    def intialize_reader(self, **kwargs):
        """ Define necessary file statistics and properties for reader
        """
        raise NotImplementedError()

    def read_file_batch(self, nskip=0, columns=None, **kwargs):
        """ Read a chunk of a tabular-data file
        Parameters
        ----------
        nskip: int
            Row offset
        columns: List[str]
            List of column names to read
        **kwargs:
            Other format-specific key-word arguments
        Returns
        -------
        A CuDF DataFrame
        """
        raise NotImplementedError()

    @property
    def estimated_row_size(self):
        return self.row_size

    def __del__(self):
        """ GPUFileReader Destructor
        """
        if self.file:
            self.file.close()


#
# GPUFileReader Sub Classes (Parquet and CSV Engines)
#


class PQFileReader(GPUFileReader):
    def intialize_reader(self, gpu_memory_frac, batch_size, **kwargs):
        self.reader = cudf.read_parquet
        self.file = open(self.file_path, "rb")

        # Read Parquet-file metadata
        (
            self.num_rows,
            self.num_row_groups,
            self.columns,
        ) = cudf.io.read_parquet_metadata(self.file)
        self.file.seek(0)
        # Use first row-group metadata to estimate memory-rqs
        # NOTE: We could also use parquet metadata here, but
        #       `total_uncompressed_size` for each column is
        #       not representitive of dataframe size for
        #       strings/categoricals (parquet only stores uniques)
        self.row_size = self.row_size or 0
        if self.num_rows > 0 and self.row_size == 0:
            for col in self.reader(self.file, num_rows=1)._columns:
                # removed logic for max in first x rows, it was
                # causing infinite loops for our customers on their datasets.
                self.row_size += col.dtype.itemsize
            self.file.seek(0)
        # Check if wwe are using row groups
        self.use_row_groups = kwargs.get("use_row_groups", None)
        self.row_group_batch = 1
        self.next_row_group = 0

        # Determine batch size if needed
        if batch_size and not self.use_row_groups:
            self.batch_size = batch_size
            self.use_row_groups = False
        else:
            # Use row size to calculate "allowable" batch size
            gpu_memory_batch = _allowable_batch_size(gpu_memory_frac, self.row_size)
            self.batch_size = min(gpu_memory_batch, self.num_rows)

            # Use row-groups if they meet memory constraints
            rg_size = int(self.num_rows / self.num_row_groups)
            if (self.use_row_groups is None) and (rg_size <= gpu_memory_batch):
                self.use_row_groups = True
            elif self.use_row_groups is None:
                self.use_row_groups = False

            # Determine row-groups per batch
            if self.use_row_groups:
                self.row_group_batch = max(int(gpu_memory_batch / rg_size), 1)

    def read_file_batch(self, nskip=0, columns=None, **kwargs):
        # not using row groups because concat uses up double memory
        # making iterator unable to use selected gpu memory fraction.
        batch = min(self.batch_size, self.num_rows - nskip)
        return self.reader(
            self.file_path,
            num_rows=batch,
            skip_rows=nskip,
            engine="cudf",
            columns=columns,
        ).reset_index(drop=True)


class CSVFileReader(GPUFileReader):
    def intialize_reader(self, gpu_memory_frac, batch_size, **kwargs):
        self.reader = cudf.read_csv
        self.file = open(self.file_path, "r")

        # Count rows and determine column names
        self.columns = []
        estimate_row_size = False
        if self.row_size is None:
            self.row_size = 0
            estimate_row_size = True

        for i, l in enumerate(self.file):
            pass
        self.file.seek(0)
        self.num_rows = i

        # Use first row to estimate memory-reqs
        names = kwargs.get("names", None)
        dtype = kwargs.get("dtype", None)
        # default csv delim is ","
        sep = kwargs.get("sep", ",")
        self.sep = sep
        self.names = []
        dtype_inf = {}
        snippet = self.reader(
            self.file,
            nrows=min(10, self.num_rows),
            names=names,
            header=False,
            dtype=dtype,
            sep=sep,
        )
        if self.num_rows > 0:
            for i, col in enumerate(snippet.columns):
                if names:
                    name = names[i]
                else:
                    name = col
                self.names.append(name)
            for i, col in enumerate(snippet._columns):
                if estimate_row_size:
                    if col.dtype == "object":
                        # Use maximum of first 10 rows
                        max_size = len(max(col.dropna())) // 2
                        self.row_size += int(max_size)
                    else:
                        self.row_size += col.dtype.itemsize
                dtype_inf[self.names[i]] = col.dtype
        self.dtype = dtype or dtype_inf

        # Determine batch size if needed
        if batch_size:
            self.batch_size = batch_size
        else:
            gpu_memory_batch = _allowable_batch_size(gpu_memory_frac, self.row_size)
            self.batch_size = min(gpu_memory_batch, self.num_rows)

    def read_file_batch(self, nskip=0, columns=None, **kwargs):
        batch = min(self.batch_size, self.num_rows - nskip)
        chunk = self.reader(
            self.file_path,
            nrows=batch,
            skiprows=nskip,
            names=self.names,
            header=False,
            sep=self.sep,
        )

        if columns:
            for col in columns:
                chunk[col] = chunk[col].astype(self.dtype[col])
            return chunk[columns]
        return chunk


#
# GPUFileIterator (Single File Iterator)
#


class GPUFileIterator:
    def __init__(
        self,
        file_path,
        engine=None,
        gpu_memory_frac=0.5,
        batch_size=None,
        columns=None,
        use_row_groups=None,
        dtypes=None,
        names=None,
        row_size=None,
        **kwargs,
    ):
        self.file_path = file_path
        self.engine = _get_read_engine(
            engine,
            file_path,
            batch_size=batch_size,
            gpu_memory_frac=gpu_memory_frac,
            use_row_groups=use_row_groups,
            dtypes=dtypes,
            names=names,
            row_size=None,
            **kwargs,
        )
        self.dtypes = dtypes
        self.columns = columns
        self.file_size = self.engine.num_rows
        self.rows_processed = 0
        self.cur_chunk = None
        self.count = 0

    def __iter__(self):
        self.rows_processed = 0
        self.count = 0
        self.cur_chunk = None
        return self

    def __len__(self):
        add_on = 0 if self.file_size % self.engine.batch_size == 0 else 1
        return self.file_size // self.engine.batch_size + add_on

    def __next__(self):
        if self.rows_processed >= self.file_size:
            if self.cur_chunk:
                chunk = self.cur_chunk
                self.cur_chunk = None
                return chunk
            raise StopIteration
        self._load_chunk()
        chunk = self.cur_chunk
        self.cur_chunk = None
        return chunk

    def _load_chunk(self):
        # retrieve missing final chunk from fileset,
        # will fail on last try before stop iteration in __next__
        if self.rows_processed < self.file_size and not self.cur_chunk:
            self.cur_chunk = self.engine.read_file_batch(
                nskip=self.rows_processed, columns=self.columns
            )
            if self.dtypes:
                self.set_dtypes()
            self.count = self.count + 1
            self.rows_processed += self.cur_chunk.shape[0]

    def set_dtypes(self):
        for col, dtype in self.dtypes.items():
            if type(dtype) is str:
                if "hex" in dtype:
                    self.cur_chunk[col] = self.cur_chunk[col]._column.nvstrings.htoi()
                    self.cur_chunk[col] = self.cur_chunk[col].astype(np.int32)
            else:
                self.cur_chunk[col] = self.cur_chunk[col].astype(dtype)


#
# GPUDatasetIterator (Iterates through multiple files)
#


class GPUDatasetIterator:
    def __init__(self, paths, **kwargs):
        if isinstance(paths, str):
            paths = [paths]
        if not isinstance(paths, list):
            raise TypeError("paths must be a string or a list.")
        if len(paths) < 1:
            raise ValueError("len(paths) must be > 0.")
        self.paths = paths
        self.num_paths = len(paths)
        self.kwargs = kwargs
        self.itr = None
        self.next_path_ind = 0

    def __iter__(self):
        self.itr = None
        self.next_path_ind = 0
        return self

    def __next__(self):

        if self.itr is None:
            self.itr = GPUFileIterator(self.paths[self.next_path_ind], **self.kwargs)
            self.next_path_ind += 1

        while True:
            try:
                return self.itr.__next__()
            except StopIteration:
                if self.next_path_ind >= self.num_paths:
                    raise StopIteration
                path = self.paths[self.next_path_ind]
                self.next_path_ind += 1
                self.itr = GPUFileIterator(path, **self.kwargs)

class Shuffler():

    def __init__(self, writer_wait_time=0.2):
        self.cont_saving = True
        self.locks_bucket = []
        self.buckets = None
        self.file_create_started = False
        self.b_idxs = None
        self.writers = []
        self.writer_files = []
        self.writer_wait_time = writer_wait_time
        
    def _start_file_writers(self, idx, out_file):
        file_created = False
        writer = None

        while True:
            if self.buckets is not None:
                if not file_created:
                    if len(self.buckets[idx]) > 0:
                        sch = self.buckets[idx][0].schema
                        writer = pq.ParquetWriter(out_file, sch)
                        self.writers.append(writer)
                        file_created = True 
                    else:
                        time.sleep(self.writer_wait_time)
                        continue

                if len(self.buckets[idx]) > 0:
                    self.locks_bucket[idx].acquire()
                    dt = self.buckets[idx].pop()
                    self.locks_bucket[idx].release()
                    writer.write_table(dt) 
                    dt = []
                elif not self.cont_saving:
                    break

            time.sleep(self.writer_wait_time)
    
    def start_writers(self, out_dir, num_out_files):
        self.out_dir = out_dir
        self.writers_thread = []
        for idx in range(num_out_files):
            out_file = os.path.join(out_dir, f"{idx}.parquet")
            self.writer_files.append(out_file)
            new_writer = threading.Thread(target=self._start_file_writers, args=(idx, out_file))
            new_writer.start()
            self.writers_thread.append(new_writer)

    def add_data(self, gdf, out_dir, num_out_files):
        if self.buckets is None:
            self.buckets = list()
            for x in range(num_out_files):
                new_elem = list()
                self.buckets.append(new_elem)
                self.locks_bucket.append(threading.Lock())

        sort_key = "__sort_index__"
        arr = cp.arange(len(gdf))
        cp.random.shuffle(arr)
        gdf[sort_key] = cudf.Series(arr)
        gdf = gdf.sort_values(sort_key).drop(columns=[sort_key])

        if not self.file_create_started:
            self.file_create_started = True
            self.start_writers(out_dir, num_out_files)

        # get slice info
        int_slice_size = gdf.shape[0] //num_out_files
        slice_size = int_slice_size if gdf.shape[0] % int_slice_size == 0 else int_slice_size + 1
        if self.b_idxs is None:
            self.b_idxs = np.arange(num_out_files)
        np.random.shuffle(self.b_idxs)

        for x in range(num_out_files):
            start = x * slice_size
            end = start + slice_size
            # check if end is over length
            end = end if end <= gdf.shape[0] else gdf.shape[0]
            to_write = gdf.iloc[cp.arange(start, end)]
            b_idx = self.b_idxs[x]
            self.locks_bucket[b_idx].acquire()
            self.buckets[b_idx].append(to_write.to_arrow())
            self.locks_bucket[b_idx].release()

    def close_writers(self):
        self.cont_saving = False
        for writer_thread in self.writers_thread:
            writer_thread.join()
        
        self.writers_thread = []
        self.buckets = []
        self.locks_bucket = []
        self.file_create_started = False

        for writer in self.writers:
            writer.close()
        self.writers = []
        self.writer_files = []
