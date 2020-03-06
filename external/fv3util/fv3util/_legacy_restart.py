from typing import Iterable
import os
import xarray as xr
import copy
from . import fortran_info
from . import io, filesystem, constants, communicator
from .quantity import Quantity
from .partitioner import CubedSpherePartitioner, get_tile_index


__all__ = ['open_restart']

RESTART_NAMES = ('fv_core.res', 'fv_srf_wnd.res', 'fv_tracer.res')
RESTART_OPTIONAL_NAMES = ('sfc_data', 'phy_data')  # not output for dycore-only runs
COUPLER_RES_NAME = 'coupler.res'


def open_restart(
        dirname: str,
        partitioner: CubedSpherePartitioner,
        comm,
        label: str = '',
        only_names: Iterable[str] = None):
    """Load restart files output by the Fortran model into a state dictionary.

    Args:
        dirname: location of restart files, can be local or remote
        partitioner: domain decomposition for this rank
        comm: mpi4py comm object
        label: prepended string on the restart files to load
        only_names (optional): list of standard names to load

    Returns:
        state: model state dictionary
    """
    tile_index = partitioner.tile_index(comm.Get_rank())
    rank = comm.Get_rank()
    state = {}
    if rank == partitioner.tile_master_rank(rank):
        for file in restart_files(dirname, tile_index, label):
            state.update(load_partial_state_from_restart_file(file, only_names=only_names))
        coupler_res_filename = get_coupler_res_filename(dirname, label)
        if filesystem.is_file(coupler_res_filename):
            with filesystem.open(coupler_res_filename, 'r') as f:
                state['time'] = io.get_current_date_from_coupler_res(f)
    state = broadcast_state(state, partitioner, comm)
    return state


def get_coupler_res_filename(dirname, label):
    return os.path.join(dirname, prepend_label(COUPLER_RES_NAME, label))


def broadcast_state(state, partitioner, comm):

    def broadcast_master():
        name_list = list(set(state.keys()).difference('time'))
        name_list = tile_comm.bcast(name_list, root=constants.MASTER_RANK)
        array_list = [state[name] for name in name_list]
        metadata_list = communicator.bcast_metadata_list(tile_comm, array_list)
        for name, array, metadata in zip(name_list, array_list, metadata_list):
            state[name] = partitioner.tile.scatter(tile_comm, array, metadata)
        comm.bcast(state.get('time', None), root=constants.MASTER_RANK)

    def broadcast_client():
        name_list = tile_comm.bcast(None, root=constants.MASTER_RANK)
        metadata_list = communicator.bcast_metadata_list(tile_comm, None)
        for name, metadata in zip(name_list, metadata_list):
            state[name] = partitioner.tile.scatter(tile_comm, None, metadata)
        metadata_list = communicator.bcast_metadata_list(tile_comm, None)
        for name, metadata in zip(name_list, metadata_list):
            state[name] = partitioner.tile.scatter(tile_comm, None, metadata)
        time = tile_comm.bcast(None, root=constants.MASTER_RANK)
        if time is not None:
            state['time'] = time

    tile_comm = comm.Split(color=partitioner.tile_index(comm.Get_rank()), key=comm.Get_rank())
    if tile_comm.Get_rank() == constants.MASTER_RANK:
        broadcast_master()
    else:
        broadcast_client()
    tile_comm.Free()
    return state


def restart_files(dirname, tile_index, label):
    for filename in restart_filenames(dirname, tile_index, label):
        with filesystem.open(filename, 'rb') as f:
            yield f


def restart_filenames(dirname, tile_index, label):
    suffix = f'.tile{tile_index + 1}.nc'
    return_list = []
    for name in RESTART_NAMES + RESTART_OPTIONAL_NAMES:
        filename = os.path.join(dirname, prepend_label(name, label) + suffix)
        if (name in RESTART_NAMES) or filesystem.is_file(filename):
            yield filename


def get_rank_suffix(rank, total_ranks):
    if total_ranks % 6 != 0:
        raise ValueError(
            f'total_ranks must be evenly divisible by 6, was given {total_ranks}'
        )
    ranks_per_tile = total_ranks // 6
    tile = get_tile_index(rank, total_ranks) + 1
    count = rank % ranks_per_tile
    if total_ranks > 6:
        rank_suffix = f'.tile{tile}.nc.{count:04}'
    else:
        rank_suffix = f'.tile{tile}.nc'
    return rank_suffix


def apply_dims(da, new_dims):
    """Applies new dimension names to the last dimensions of the given DataArray."""
    return da.rename(dict(zip(da.dims[-len(new_dims):], new_dims)))


def apply_restart_metadata(state):
    new_state = {}
    for name, da in state.items():
        if name in fortran_info.properties_by_std_name:
            properties = fortran_info.properties_by_std_name[name]
            new_dims = properties['dims']
            new_state[name] = apply_dims(da, new_dims)
            new_state[name].attrs['units'] = properties['units']
        else:
            new_state[name] = copy.deepcopy(da)
    return new_state


def map_keys(old_dict, old_keys_to_new):
    new_dict = {}
    for old_key, new_key in old_keys_to_new.items():
        if old_key in old_dict:
            new_dict[new_key] = old_dict[old_key]
    for old_key in set(old_dict.keys()).difference(old_keys_to_new.keys()):
        new_dict[old_key] = old_dict[old_key]
    return new_dict


def prepend_label(filename, label=None):
    if label is not None:
        return f'{label}.{filename}'
    else:
        return filename


def load_partial_state_from_restart_file(file, only_names=None):
    ds = xr.open_dataset(file).isel(Time=0).drop("Time")
    state = map_keys(ds.data_vars, fortran_info.get_restart_standard_names())
    state = apply_restart_metadata(state)
    if only_names is None:
        only_names = state.keys()
    state = {  # remove any variables that don't have restart metadata
        name: value for name, value in state.items()
        if ((name == 'time') or ('units' in value.attrs)) and name in only_names
    }
    for name, array in state.items():
        if name != 'time':
            array.load()
            state[name] = Quantity.from_data_array(array)
