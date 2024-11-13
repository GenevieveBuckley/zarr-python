import hypothesis.extra.numpy as npst
import hypothesis.strategies as st
import pytest
from hypothesis import assume, note
from hypothesis.stateful import (
    RuleBasedStateMachine,
    Settings,
    initialize,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)

import zarr
from zarr import Array
from zarr.abc.store import Store
from zarr.core.sync import SyncMixin
from zarr.storage import MemoryStore, ZipStore
from zarr.testing.strategies import node_names, np_array_and_chunks, numpy_arrays


def split_prefix_name(path):
    split = path.rsplit("/", maxsplit=1)
    if len(split) > 1:
        prefix, name = split
    else:
        prefix = ""
        (name,) = split
    return prefix, name


class ZarrHierarchyStateMachine(SyncMixin, RuleBasedStateMachine):
    """
    This state machine models operations that modify a zarr store's
    hierarchy. That is, user actions that modify arrays/groups as well
    as list operations. It is intended to be used by external stores, and
    compares their results to a MemoryStore that is assumed to be perfect.
    """

    def __init__(self, store) -> None:
        super().__init__()

        self.store = store

        self.model = MemoryStore()
        zarr.group(store=self.model)

        # Track state of the hierarchy, these should contain fully qualified paths
        self.all_groups = set()
        self.all_arrays = set()

    @initialize()
    def init_store(self):
        # This lets us reuse the fixture provided store.
        self._sync(self.store.clear())
        zarr.group(store=self.store)

    def can_add(self, path):
        return path not in self.all_groups and path not in self.all_arrays

    # -------------------- store operations -----------------------
    @rule(name=node_names, data=st.data())
    def add_group(self, name, data):
        if self.all_groups:
            parent = data.draw(st.sampled_from(sorted(self.all_groups)), label="Group parent")
        else:
            parent = ""
        path = f"{parent}/{name}".lstrip("/")
        assume(self.can_add(path))
        note(f"Adding group: path='{path}'")
        self.all_groups.add(path)
        zarr.group(store=self.store, path=path)
        zarr.group(store=self.model, path=path)

    @rule(
        data=st.data(),
        name=node_names,
        array_and_chunks=np_array_and_chunks(arrays=numpy_arrays(zarr_formats=st.just(3))),
    )
    def add_array(self, data, name, array_and_chunks):
        array, chunks = array_and_chunks
        fill_value = data.draw(npst.from_dtype(array.dtype))
        if self.all_groups:
            parent = data.draw(st.sampled_from(sorted(self.all_groups)), label="Array parent")
        else:
            parent = ""
        # TODO: support creating deeper paths
        # TODO: support overwriting potentially by just skipping `self.can_add`
        path = f"{parent}/{name}".lstrip("/")
        assume(self.can_add(path))
        note(f"Adding array:  path='{path}'  shape={array.shape}  chunks={chunks}")
        for store in [self.store, self.model]:
            zarr.array(array, chunks=chunks, path=path, store=store, fill_value=fill_value)
        self.all_arrays.add(path)

    # @precondition(lambda self: bool(self.all_groups))
    # @precondition(lambda self: bool(self.all_arrays))
    # @rule(data=st.data())
    # def move_array(self, data):
    #     array_path = data.draw(st.sampled_from(self.all_arrays), label="Array move source")
    #     to_group = data.draw(st.sampled_from(self.all_groups), label="Array move destination")

    #     # fixme renaiming to self?
    #     array_name = os.path.basename(array_path)
    #     assume(self.model.can_add(to_group, array_name))
    #     new_path = f"{to_group}/{array_name}".lstrip("/")
    #     note(f"moving array '{array_path}' -> '{new_path}'")
    #     self.model.rename(array_path, new_path)
    #     self.repo.store.rename(array_path, new_path)

    # @precondition(lambda self: len(self.all_groups) >= 2)
    # @rule(data=st.data())
    # def move_group(self, data):
    #     from_group = data.draw(st.sampled_from(self.all_groups), label="Group move source")
    #     to_group = data.draw(st.sampled_from(self.all_groups), label="Group move destination")
    #     assume(not to_group.startswith(from_group))

    #     from_group_name = os.path.basename(from_group)
    #     assume(self.model.can_add(to_group, from_group_name))
    #     # fixme renaiming to self?
    #     new_path = f"{to_group}/{from_group_name}".lstrip("/")
    #     note(f"moving group '{from_group}' -> '{new_path}'")
    #     self.model.rename(from_group, new_path)
    #     self.repo.store.rename(from_group, new_path)

    @precondition(lambda self: len(self.all_arrays) >= 1)
    @rule(data=st.data())
    def delete_array_using_del(self, data):
        array_path = data.draw(
            st.sampled_from(sorted(self.all_arrays)), label="Array deletion target"
        )
        prefix, array_name = split_prefix_name(array_path)
        note(f"Deleting array '{array_path}' ({prefix=!r}, {array_name=!r}) using del")
        for store in [self.model, self.store]:
            group = zarr.open_group(path=prefix, store=store)
            group[array_name]  # check that it exists
            del group[array_name]
        self.all_arrays.remove(array_path)

    @precondition(lambda self: len(self.all_groups) >= 2)  # fixme don't delete root
    @rule(data=st.data())
    def delete_group_using_del(self, data):
        group_path = data.draw(
            st.sampled_from(sorted(self.all_groups)), label="Group deletion target"
        )
        prefix, group_name = split_prefix_name(group_path)
        note(f"Deleting group '{group_path=!r}', {prefix=!r}, {group_name=!r} using delete")
        members = zarr.open_group(store=self.model, path=group_path).members(max_depth=None)
        for _, obj in members:
            if isinstance(obj, Array):
                self.all_arrays.remove(obj.path)
            else:
                self.all_groups.remove(obj.path)
        for store in [self.store, self.model]:
            group = zarr.open_group(store=store, path=prefix)
            group[group_name]  # check that it exists
            del group[group_name]
        if group_path != "/":
            # The root group is always present
            self.all_groups.remove(group_path)

    # # --------------- assertions -----------------
    # def check_group_arrays(self, group):
    #     # note(f"Checking arrays of '{group}'")
    #     g1 = self.model.get_group(group)
    #     g2 = zarr.open_group(path=group, mode="r", store=self.repo.store)
    #     model_arrays = sorted(g1.arrays(), key=itemgetter(0))
    #     our_arrays = sorted(g2.arrays(), key=itemgetter(0))
    #     for (n1, a1), (n2, a2) in zip_longest(model_arrays, our_arrays):
    #         assert n1 == n2
    #         assert_array_equal(a1, a2)

    # def check_subgroups(self, group_path):
    #     g1 = self.model.get_group(group_path)
    #     g2 = zarr.open_group(path=group_path, mode="r", store=self.repo.store)
    #     g1_children = [name for (name, _) in g1.groups()]
    #     g2_children = [name for (name, _) in g2.groups()]
    #     # note(f"Checking {len(g1_children)} subgroups of group '{group_path}'")
    #     assert g1_children == g2_children

    # def check_list_prefix_from_group(self, group):
    #     prefix = f"meta/root/{group}"
    #     model_list = sorted(self.model.list_prefix(prefix))
    #     al_list = sorted(self.repo.store.list_prefix(prefix))
    #     # note(f"Checking {len(model_list)} keys under '{prefix}'")
    #     assert model_list == al_list

    #     prefix = f"data/root/{group}"
    #     model_list = sorted(self.model.list_prefix(prefix))
    #     al_list = sorted(self.repo.store.list_prefix(prefix))
    #     # note(f"Checking {len(model_list)} keys under '{prefix}'")
    #     assert model_list == al_list

    # @precondition(lambda self: self.model.is_persistent_session())
    # @rule(data=st.data())
    # def check_group_path(self, data):
    #     t0 = time.time()
    #     group = data.draw(st.sampled_from(self.all_groups))
    #     self.check_list_prefix_from_group(group)
    #     self.check_subgroups(group)
    #     self.check_group_arrays(group)
    #     t1 = time.time()
    #     note(f"Checks took {t1 - t0} sec.")

    @invariant()
    def check_list_prefix_from_root(self):
        model_list = self._sync_iter(self.model.list_prefix(""))
        store_list = self._sync_iter(self.store.list_prefix(""))
        note(f"Checking {len(model_list)} keys")
        assert sorted(model_list) == sorted(store_list)


def test_zarr_hierarchy(sync_store: Store):
    def mk_test_instance_sync() -> ZarrHierarchyStateMachine:
        return ZarrHierarchyStateMachine(sync_store)

    if isinstance(sync_store, ZipStore):
        pytest.skip(reason="ZipStore does not support delete")
    if isinstance(sync_store, MemoryStore):
        run_state_machine_as_test(
            mk_test_instance_sync, settings=Settings(report_multiple_bugs=False)
        )