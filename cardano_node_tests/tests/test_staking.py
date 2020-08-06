import logging
from pathlib import Path
from typing import List

import hypothesis
import hypothesis.strategies as st
import pytest
from _pytest.fixtures import FixtureRequest

from cardano_node_tests.utils import clusterlib
from cardano_node_tests.utils import helpers

LOGGER = logging.getLogger(__name__)


@pytest.fixture(scope="module")
def temp_dir(tmp_path_factory):
    """Create a temporary dir and change to it."""
    tmp_path = tmp_path_factory.mktemp("test_staking")
    with helpers.change_cwd(tmp_path):
        yield tmp_path


@pytest.fixture(scope="class")
def update_pool_cost(cluster_class: clusterlib.ClusterLib):
    """Update "minPoolCost" to 5000."""
    helpers.update_params(
        cluster_obj=cluster_class,
        cli_arg="--min-pool-cost",
        param_name="minPoolCost",
        param_value=5000,
    )


# use the "temp_dir" fixture for all tests automatically
pytestmark = pytest.mark.usefixtures("temp_dir")


def _create_pool_owners(
    cluster_obj: clusterlib.ClusterLib, temp_template: str, no_of_addr: int = 1,
) -> List[clusterlib.PoolOwner]:
    """Create PoolOwners.

    Common functionality for tests.
    """
    pool_owners = []
    payment_addrs = []
    for i in range(no_of_addr):
        # create key pairs and addresses
        stake_addr_rec = helpers.create_stake_addr_records(
            f"addr{i}_{temp_template}", cluster_obj=cluster_obj
        )[0]
        payment_addr_rec = helpers.create_payment_addr_records(
            f"addr{i}_{temp_template}",
            cluster_obj=cluster_obj,
            stake_vkey_file=stake_addr_rec.vkey_file,
        )[0]
        # create pool owner struct
        pool_owner = clusterlib.PoolOwner(payment=payment_addr_rec, stake=stake_addr_rec)
        payment_addrs.append(payment_addr_rec)
        pool_owners.append(pool_owner)

    return pool_owners


def _check_staking(
    pool_owners: List[clusterlib.PoolOwner],
    cluster_obj: clusterlib.ClusterLib,
    stake_pool_id: str,
    pool_data: clusterlib.PoolData,
):
    """Check that pool and staking were correctly setup."""
    LOGGER.info("Waiting up to 2 epochs for stake pool to be registered.")
    helpers.wait_for(
        lambda: stake_pool_id in cluster_obj.get_stake_distribution(),
        delay=10,
        num_sec=2 * cluster_obj.epoch_length,
        message="register stake pool",
    )

    # check that the pool was correctly registered on chain
    pool_ledger_state = cluster_obj.get_registered_stake_pools_ledger_state().get(stake_pool_id)
    assert pool_ledger_state, (
        "The newly created stake pool id is not shown inside the available stake pools;\n"
        f"Pool ID: {stake_pool_id} vs Existing IDs: "
        f"{list(cluster_obj.get_registered_stake_pools_ledger_state())}"
    )
    assert not helpers.check_pool_data(pool_ledger_state, pool_data)

    for owner in pool_owners:
        stake_addr_info = cluster_obj.get_stake_addr_info(owner.stake.address)

        # check that the stake address was delegated
        assert (
            stake_addr_info and stake_addr_info.delegation
        ), f"Stake address was not delegated yet: {stake_addr_info}"

        assert stake_pool_id == stake_addr_info.delegation, "Stake address delegated to wrong pool"

        # TODO: change this once 'stake_addr_info' contain stake address, not hash
        assert (
            # strip 'e0' from the beginning of the address hash
            stake_addr_info.addr_hash[2:]
            in pool_ledger_state["owners"]
        ), "'owner' value is different than expected"


def _create_register_pool_delegate_stake_tx(
    cluster_obj: clusterlib.ClusterLib,
    pool_owners: List[clusterlib.PoolOwner],
    temp_template: str,
    pool_data: clusterlib.PoolData,
):
    """Create and register a stake pool, delegate stake address - all in single TX.

    Common functionality for tests.
    """
    # create node VRF key pair
    node_vrf = cluster_obj.gen_vrf_key_pair(node_name=pool_data.pool_name)
    # create node cold key pair and counter
    node_cold = cluster_obj.gen_cold_key_pair_and_counter(node_name=pool_data.pool_name)

    # create stake address registration certs
    stake_addr_reg_cert_files = [
        cluster_obj.gen_stake_addr_registration_cert(
            addr_name=f"addr{i}_{temp_template}", stake_vkey_file=p.stake.vkey_file
        )
        for i, p in enumerate(pool_owners)
    ]

    # create stake address delegation cert
    stake_addr_deleg_cert_files = [
        cluster_obj.gen_stake_addr_delegation_cert(
            addr_name=f"addr{i}_{temp_template}",
            stake_vkey_file=p.stake.vkey_file,
            node_cold_vkey_file=node_cold.vkey_file,
        )
        for i, p in enumerate(pool_owners)
    ]

    # create stake pool registration cert
    pool_reg_cert_file = cluster_obj.gen_pool_registration_cert(
        pool_data=pool_data,
        node_vrf_vkey_file=node_vrf.vkey_file,
        node_cold_vkey_file=node_cold.vkey_file,
        owner_stake_vkey_files=[p.stake.vkey_file for p in pool_owners],
    )

    tx_files = clusterlib.TxFiles(
        certificate_files=[
            pool_reg_cert_file,
            *stake_addr_reg_cert_files,
            *stake_addr_deleg_cert_files,
        ],
        signing_key_files=[
            *[p.payment.skey_file for p in pool_owners],
            *[p.stake.skey_file for p in pool_owners],
            node_cold.skey_file,
        ],
    )

    src_address = pool_owners[0].payment.address
    src_init_balance = cluster_obj.get_address_balance(src_address)

    # register and delegate stake address, create and register pool
    tx_raw_data = cluster_obj.send_tx(src_address=src_address, tx_files=tx_files)
    cluster_obj.wait_for_new_block(new_blocks=2)

    # check that the balance for source address was correctly updated
    assert (
        cluster_obj.get_address_balance(src_address)
        == src_init_balance
        - tx_raw_data.fee
        - len(pool_owners) * cluster_obj.get_key_deposit()
        - cluster_obj.get_pool_deposit()
    ), f"Incorrect balance for source address `{src_address}`"

    # check that pool and staking were correctly setup
    stake_pool_id = cluster_obj.get_stake_pool_id(node_cold.vkey_file)
    _check_staking(
        pool_owners, cluster_obj=cluster_obj, stake_pool_id=stake_pool_id, pool_data=pool_data,
    )


def _create_register_pool_tx_delegate_stake_tx(
    cluster_obj: clusterlib.ClusterLib,
    pool_owners: List[clusterlib.PoolOwner],
    temp_template: str,
    pool_data: clusterlib.PoolData,
) -> clusterlib.PoolCreationArtifacts:
    """Create and register a stake pool - first TX; delegate stake address - second TX.

    Common functionality for tests.
    """
    # create and register pool
    pool_artifacts = cluster_obj.create_stake_pool(pool_data=pool_data, pool_owners=pool_owners)

    # create stake address registration certs
    stake_addr_reg_cert_files = [
        cluster_obj.gen_stake_addr_registration_cert(
            addr_name=f"addr{i}_{temp_template}", stake_vkey_file=p.stake.vkey_file
        )
        for i, p in enumerate(pool_owners)
    ]

    # create stake address delegation cert
    stake_addr_deleg_cert_files = [
        cluster_obj.gen_stake_addr_delegation_cert(
            addr_name=f"addr{i}_{temp_template}",
            stake_vkey_file=p.stake.vkey_file,
            node_cold_vkey_file=pool_artifacts.cold_key_pair_and_counter.vkey_file,
        )
        for i, p in enumerate(pool_owners)
    ]

    tx_files = clusterlib.TxFiles(
        certificate_files=[*stake_addr_reg_cert_files, *stake_addr_deleg_cert_files],
        signing_key_files=[
            *[p.payment.skey_file for p in pool_owners],
            *[p.stake.skey_file for p in pool_owners],
            pool_artifacts.cold_key_pair_and_counter.skey_file,
        ],
    )

    src_address = pool_owners[0].payment.address
    src_init_balance = cluster_obj.get_address_balance(src_address)

    # register and delegate stake address
    tx_raw_data = cluster_obj.send_tx(src_address=src_address, tx_files=tx_files)
    cluster_obj.wait_for_new_block(new_blocks=2)

    # check that the balance for source address was correctly updated
    assert (
        cluster_obj.get_address_balance(src_address)
        == src_init_balance - tx_raw_data.fee - len(pool_owners) * cluster_obj.get_key_deposit()
    ), f"Incorrect balance for source address `{src_address}`"

    # check that pool and staking were correctly setup
    _check_staking(
        pool_owners,
        cluster_obj=cluster_obj,
        stake_pool_id=pool_artifacts.stake_pool_id,
        pool_data=pool_data,
    )

    return pool_artifacts


def _delegate_addr_using_cert(
    cluster_obj: clusterlib.ClusterLib,
    addrs_data: dict,
    temp_template: str,
    request: FixtureRequest,
):
    """Submit registration certificate and delegate to pool using certificate."""
    node_cold = addrs_data["node-pool1"]["cold_key_pair"]

    # create key pairs and addresses
    stake_addr_rec = helpers.create_stake_addr_records(
        f"addr0_{temp_template}", cluster_obj=cluster_obj
    )[0]
    payment_addr_rec = helpers.create_payment_addr_records(
        f"addr0_{temp_template}", cluster_obj=cluster_obj, stake_vkey_file=stake_addr_rec.vkey_file,
    )[0]

    pool_owner = clusterlib.PoolOwner(payment=payment_addr_rec, stake=stake_addr_rec)

    # create stake address registration cert
    stake_addr_reg_cert_file = cluster_obj.gen_stake_addr_registration_cert(
        addr_name=f"addr0_{temp_template}", stake_vkey_file=stake_addr_rec.vkey_file
    )
    # create stake address delegation cert
    stake_addr_deleg_cert_file = cluster_obj.gen_stake_addr_delegation_cert(
        addr_name=f"addr0_{temp_template}",
        stake_vkey_file=stake_addr_rec.vkey_file,
        node_cold_vkey_file=node_cold.vkey_file,
    )

    # fund source address
    helpers.fund_from_faucet(
        payment_addr_rec, cluster_obj=cluster_obj, faucet_data=addrs_data["user1"], request=request,
    )

    tx_files = clusterlib.TxFiles(
        certificate_files=[stake_addr_reg_cert_file, stake_addr_deleg_cert_file],
        signing_key_files=[payment_addr_rec.skey_file, stake_addr_rec.skey_file],
    )

    src_address = payment_addr_rec.address
    src_init_balance = cluster_obj.get_address_balance(src_address)

    # register stake address and delegate it to pool
    tx_raw_data = cluster_obj.send_tx(src_address=src_address, tx_files=tx_files)
    cluster_obj.wait_for_new_block(new_blocks=2)

    # check that the balance for source address was correctly updated
    assert (
        cluster_obj.get_address_balance(src_address)
        == src_init_balance - tx_raw_data.fee - cluster_obj.get_key_deposit()
    ), f"Incorrect balance for source address `{src_address}`"

    helpers.wait_for_stake_distribution(cluster_obj)

    # check that the stake address was delegated
    stake_addr_info = cluster_obj.get_stake_addr_info(stake_addr_rec.address)
    assert (
        stake_addr_info and stake_addr_info.delegation
    ), f"Stake address was not delegated yet: {stake_addr_info}"

    stake_pool_id = cluster_obj.get_stake_pool_id(node_cold.vkey_file)
    assert stake_pool_id == stake_addr_info.delegation, "Stake address delegated to wrong pool"

    return pool_owner


class TestDelegateAddr:
    def test_delegate_using_addr(
        self,
        cluster_session: clusterlib.ClusterLib,
        addrs_data_session: dict,
        request: FixtureRequest,
    ):
        """Submit registration certificate and delegate to pool using stake address."""
        cluster = cluster_session
        temp_template = "test_delegate_using_addr"

        # create key pairs and addresses
        stake_addr_rec = helpers.create_stake_addr_records(
            f"addr0_{temp_template}", cluster_obj=cluster
        )[0]
        payment_addr_rec = helpers.create_payment_addr_records(
            f"addr0_{temp_template}", cluster_obj=cluster, stake_vkey_file=stake_addr_rec.vkey_file,
        )[0]
        stake_addr_reg_cert_file = cluster.gen_stake_addr_registration_cert(
            addr_name=f"addr0_{temp_template}", stake_vkey_file=stake_addr_rec.vkey_file
        )

        # fund source address
        helpers.fund_from_faucet(
            payment_addr_rec,
            cluster_obj=cluster,
            faucet_data=addrs_data_session["user1"],
            request=request,
        )

        tx_files = clusterlib.TxFiles(
            certificate_files=[stake_addr_reg_cert_file],
            signing_key_files=[payment_addr_rec.skey_file, stake_addr_rec.skey_file],
        )

        src_address = payment_addr_rec.address
        src_init_balance = cluster.get_address_balance(src_address)

        # register stake address
        tx_raw_data = cluster.send_tx(src_address=src_address, tx_files=tx_files)
        cluster.wait_for_new_block(new_blocks=2)

        # check that the balance for source address was correctly updated
        assert (
            cluster.get_address_balance(src_address)
            == src_init_balance - tx_raw_data.fee - cluster.get_key_deposit()
        ), f"Incorrect balance for source address `{src_address}`"
        src_register_balance = cluster.get_address_balance(src_address)

        first_pool_id_in_stake_dist = list(helpers.wait_for_stake_distribution(cluster))[0]
        delegation_fee = cluster.calculate_tx_fee(src_address=src_address, tx_files=tx_files)

        # delegate the stake address to pool
        # TODO: remove try..catch once the functionality is implemented
        try:
            cluster.delegate_stake_addr(
                stake_addr_skey=stake_addr_rec.skey_file,
                pool_id=first_pool_id_in_stake_dist,
                delegation_fee=delegation_fee,
            )
        except clusterlib.CLIError as exc:
            if "command not implemented yet" in str(exc):
                pytest.xfail(
                    "Delegating stake address using `cardano-cli shelley stake-address delegate` "
                    "not implemented yet."
                )
        cluster.wait_for_new_block(new_blocks=2)

        # check that the balance for source address was correctly updated
        assert (
            cluster.get_address_balance(src_address) == src_register_balance - delegation_fee
        ), f"Incorrect balance for source address `{src_address}`"

        helpers.wait_for_stake_distribution(cluster)

        # check that the stake address was delegated
        stake_addr_info = cluster.get_stake_addr_info(stake_addr_rec.address)
        assert (
            stake_addr_info and stake_addr_info.delegation
        ), f"Stake address was not delegated yet: {stake_addr_info}"

        assert (
            first_pool_id_in_stake_dist == stake_addr_info.delegation
        ), "Stake address delegated to wrong pool"

    def test_delegate_using_cert(
        self,
        cluster_session: clusterlib.ClusterLib,
        addrs_data_session: dict,
        request: FixtureRequest,
    ):
        """Submit registration certificate and delegate to pool using certificate."""
        cluster = cluster_session
        temp_template = "test_delegate_using_cert"

        # submit registration certificate and delegate to pool using certificate
        _delegate_addr_using_cert(
            cluster_obj=cluster,
            addrs_data=addrs_data_session,
            temp_template=temp_template,
            request=request,
        )

    def test_deregister_using_cert(
        self,
        cluster_session: clusterlib.ClusterLib,
        addrs_data_session: dict,
        request: FixtureRequest,
    ):
        """De-register stake address."""
        cluster = cluster_session
        temp_template = "test_deregister_addr"

        # submit registration certificate and delegate to pool using certificate
        pool_user = _delegate_addr_using_cert(
            cluster_obj=cluster,
            addrs_data=addrs_data_session,
            temp_template=temp_template,
            request=request,
        )

        stake_addr_dereg_cert = cluster.gen_stake_addr_deregistration_cert(
            addr_name=f"addr0_{temp_template}", stake_vkey_file=pool_user.stake.vkey_file
        )

        tx_files = clusterlib.TxFiles(
            certificate_files=[stake_addr_dereg_cert],
            signing_key_files=[pool_user.payment.skey_file, pool_user.stake.skey_file],
        )

        src_address = pool_user.payment.address
        src_init_balance = cluster.get_address_balance(src_address)

        # de-register stake address
        tx_raw_data = cluster.send_tx(src_address=src_address, tx_files=tx_files)
        cluster.wait_for_new_block(new_blocks=2)

        # check that the key deposit was returned
        assert (
            cluster.get_address_balance(src_address)
            == src_init_balance - tx_raw_data.fee + cluster.get_key_deposit()
        ), f"Incorrect balance for source address `{src_address}`"

        helpers.wait_for_stake_distribution(cluster)

        # check that the stake address is no longer delegated
        stake_addr_info = cluster.get_stake_addr_info(pool_user.stake.address)
        assert stake_addr_info is None, f"Stake address is still delegated: {stake_addr_info}"


class TestStakePool:
    @pytest.mark.parametrize("no_of_addr", [1, 3])
    def test_stake_pool_metadata(
        self,
        cluster_session: clusterlib.ClusterLib,
        addrs_data_session: dict,
        temp_dir: Path,
        no_of_addr: int,
        request: FixtureRequest,
    ):
        """Create and register a stake pool with metadata."""
        cluster = cluster_session
        temp_template = f"test_stake_pool_metadata_{no_of_addr}owners"

        pool_metadata = {
            "name": "QA E2E test",
            "description": "Shelley QA E2E test Test",
            "ticker": "QA1",
            "homepage": "www.test1.com",
        }
        pool_metadata_file = helpers.write_json(
            temp_dir / f"poolY_{no_of_addr}_registration_metadata.json", pool_metadata
        )

        pool_data = clusterlib.PoolData(
            pool_name=f"poolY_{no_of_addr}",
            pool_pledge=1000,
            pool_cost=15,
            pool_margin=0.2,
            pool_metadata_url="https://www.where_metadata_file_is_located.com",
            pool_metadata_hash=cluster.gen_pool_metadata_hash(pool_metadata_file),
        )

        # create pool owners
        pool_owners = _create_pool_owners(
            cluster_obj=cluster, temp_template=temp_template, no_of_addr=no_of_addr,
        )

        # fund source address
        helpers.fund_from_faucet(
            pool_owners[0].payment,
            cluster_obj=cluster,
            faucet_data=addrs_data_session["user1"],
            amount=900_000_000,
            request=request,
        )

        # register pool and delegate stake address
        _create_register_pool_delegate_stake_tx(
            cluster_obj=cluster,
            pool_owners=pool_owners,
            temp_template=temp_template,
            pool_data=pool_data,
        )

    @pytest.mark.parametrize("no_of_addr", [1, 3])
    def test_create_stake_pool(
        self,
        cluster_session: clusterlib.ClusterLib,
        addrs_data_session: dict,
        no_of_addr: int,
        request: FixtureRequest,
    ):
        """Create and register a stake pool."""
        cluster = cluster_session
        temp_template = f"test_stake_pool_{no_of_addr}owners"

        pool_data = clusterlib.PoolData(
            pool_name=f"poolX_{no_of_addr}",
            pool_pledge=12345,
            pool_cost=123456789,
            pool_margin=0.123,
        )

        # create pool owners
        pool_owners = _create_pool_owners(
            cluster_obj=cluster, temp_template=temp_template, no_of_addr=no_of_addr,
        )

        # fund source address
        helpers.fund_from_faucet(
            pool_owners[0].payment,
            cluster_obj=cluster,
            faucet_data=addrs_data_session["user1"],
            amount=900_000_000,
            request=request,
        )

        # register pool and delegate stake address
        _create_register_pool_delegate_stake_tx(
            cluster_obj=cluster,
            pool_owners=pool_owners,
            temp_template=temp_template,
            pool_data=pool_data,
        )

    @pytest.mark.parametrize("no_of_addr", [1, 3])
    def test_deregister_stake_pool(
        self,
        cluster_session: clusterlib.ClusterLib,
        addrs_data_session: dict,
        temp_dir: Path,
        no_of_addr: int,
        request: FixtureRequest,
    ):
        """Deregister stake pool."""
        cluster = cluster_session
        temp_template = f"test_deregister_stake_pool_{no_of_addr}owners"

        pool_metadata = {
            "name": "QA E2E test",
            "description": "Shelley QA E2E test Test",
            "ticker": "QA1",
            "homepage": "www.test1.com",
        }
        pool_metadata_file = helpers.write_json(
            temp_dir / f"poolZ_{no_of_addr}_registration_metadata.json", pool_metadata
        )

        pool_data = clusterlib.PoolData(
            pool_name=f"poolZ_{no_of_addr}",
            pool_pledge=222,
            pool_cost=123,
            pool_margin=0.512,
            pool_metadata_url="https://www.where_metadata_file_is_located.com",
            pool_metadata_hash=cluster.gen_pool_metadata_hash(pool_metadata_file),
        )

        # create pool owners
        pool_owners = _create_pool_owners(
            cluster_obj=cluster, temp_template=temp_template, no_of_addr=no_of_addr,
        )

        # fund source address
        helpers.fund_from_faucet(
            pool_owners[0].payment,
            cluster_obj=cluster,
            faucet_data=addrs_data_session["user1"],
            amount=900_000_000,
            request=request,
        )

        # register pool and delegate stake address
        pool_artifacts = _create_register_pool_tx_delegate_stake_tx(
            cluster_obj=cluster,
            pool_owners=pool_owners,
            temp_template=temp_template,
            pool_data=pool_data,
        )

        pool_owner = pool_owners[0]
        src_register_balance = cluster.get_address_balance(pool_owner.payment.address)

        src_register_stake_addr_info = cluster.get_stake_addr_info(pool_owner.stake.address)
        src_register_reward = (
            src_register_stake_addr_info.reward_account_balance
            if src_register_stake_addr_info
            else 0
        )

        # deregister stake pool
        __, tx_raw_data = cluster.deregister_stake_pool(
            pool_owners=pool_owners,
            node_cold_key_pair=pool_artifacts.cold_key_pair_and_counter,
            epoch=cluster.get_last_block_epoch() + 1,
            pool_name=pool_data.pool_name,
        )

        LOGGER.info("Waiting up to 3 epochs for stake pool to be deregistered.")
        helpers.wait_for(
            lambda: pool_artifacts.stake_pool_id not in cluster.get_stake_distribution(),
            delay=10,
            num_sec=3 * cluster.epoch_length,
            message="deregister stake pool",
        )

        # check that the balance for source address was correctly updated
        assert src_register_balance - tx_raw_data.fee == cluster.get_address_balance(
            pool_owner.payment.address
        )

        # check that the deposit was returned to reward account
        stake_addr_info = cluster.get_stake_addr_info(pool_owner.stake.address)
        assert (
            stake_addr_info
            and stake_addr_info.reward_account_balance
            == src_register_reward + cluster.get_pool_deposit()
        )

        # check that the pool was correctly de-registered on chain
        pool_ledger_state = cluster.get_registered_stake_pools_ledger_state().get(
            pool_artifacts.stake_pool_id
        )
        assert not pool_ledger_state, (
            "The de-registered stake pool id is still shown inside the available stake pools;\n"
            f"Pool ID: {pool_artifacts.stake_pool_id} vs Existing IDs: "
            f"{list(cluster.get_registered_stake_pools_ledger_state())}"
        )

        for owner_rec in pool_owners:
            stake_addr_info = cluster.get_stake_addr_info(owner_rec.stake.address)

            # check that the stake address is no longer delegated
            assert (
                stake_addr_info and not stake_addr_info.delegation
            ), f"Stake address is still delegated: {stake_addr_info}"

    @pytest.mark.parametrize("no_of_addr", [1, 2])
    def test_update_stake_pool_metadata(
        self,
        cluster_session: clusterlib.ClusterLib,
        addrs_data_session: dict,
        temp_dir: Path,
        no_of_addr: int,
        request: FixtureRequest,
    ):
        """Update stake pool metadata."""
        cluster = cluster_session
        temp_template = f"test_update_stake_pool_metadata_{no_of_addr}owners"

        pool_metadata = {
            "name": "QA E2E test",
            "description": "Shelley QA E2E test Test",
            "ticker": "QA1",
            "homepage": "www.test1.com",
        }
        pool_metadata_file = helpers.write_json(
            temp_dir / f"poolA_{no_of_addr}_registration_metadata.json", pool_metadata
        )

        pool_metadata_updated = {
            "name": "QA_test_pool",
            "description": "pool description update",
            "ticker": "QA22",
            "homepage": "www.qa22.com",
        }
        pool_metadata_updated_file = helpers.write_json(
            temp_dir / f"poolA_{no_of_addr}_registration_metadata_updated.json",
            pool_metadata_updated,
        )

        pool_data = clusterlib.PoolData(
            pool_name=f"poolA_{no_of_addr}",
            pool_pledge=4567,
            pool_cost=3,
            pool_margin=0.01,
            pool_metadata_url="https://init_location.com",
            pool_metadata_hash=cluster.gen_pool_metadata_hash(pool_metadata_file),
        )

        pool_data_updated = pool_data._replace(
            pool_metadata_url="https://www.updated_location.com",
            pool_metadata_hash=cluster.gen_pool_metadata_hash(pool_metadata_updated_file),
        )

        # create pool owners
        pool_owners = _create_pool_owners(
            cluster_obj=cluster, temp_template=temp_template, no_of_addr=no_of_addr,
        )

        # fund source address
        helpers.fund_from_faucet(
            pool_owners[0].payment,
            cluster_obj=cluster,
            faucet_data=addrs_data_session["user1"],
            amount=900_000_000,
            request=request,
        )

        # register pool and delegate stake address
        pool_artifacts = _create_register_pool_tx_delegate_stake_tx(
            cluster_obj=cluster,
            pool_owners=pool_owners,
            temp_template=temp_template,
            pool_data=pool_data,
        )

        # update the pool parameters by resubmitting the pool registration certificate
        cluster.register_stake_pool(
            pool_data=pool_data_updated,
            pool_owners=pool_owners,
            node_vrf_vkey_file=pool_artifacts.vrf_key_pair.vkey_file,
            node_cold_key_pair=pool_artifacts.cold_key_pair_and_counter,
            deposit=0,  # no additional deposit, the pool is already registered
        )
        cluster.wait_for_new_epoch()

        # check that the pool has it's original ID after updating the metadata
        new_stake_pool_id = cluster.get_stake_pool_id(
            pool_artifacts.cold_key_pair_and_counter.vkey_file
        )
        assert (
            pool_artifacts.stake_pool_id == new_stake_pool_id
        ), "New pool ID was generated after updating the pool metadata"

        # check that the pool parameters were correctly updated on chain
        updated_pool_ledger_state = (
            cluster.get_registered_stake_pools_ledger_state().get(pool_artifacts.stake_pool_id)
            or {}
        )
        assert not helpers.check_pool_data(updated_pool_ledger_state, pool_data_updated)

    @pytest.mark.parametrize("no_of_addr", [1, 2])
    def test_update_stake_pool_parameters(
        self,
        cluster_session: clusterlib.ClusterLib,
        addrs_data_session: dict,
        temp_dir: Path,
        no_of_addr: int,
        request: FixtureRequest,
    ):
        """Update stake pool parameters."""
        cluster = cluster_session
        temp_template = f"test_update_stake_pool_{no_of_addr}owners"

        pool_metadata = {
            "name": "QA E2E test",
            "description": "Shelley QA E2E test Test",
            "ticker": "QA1",
            "homepage": "www.test1.com",
        }
        pool_metadata_file = helpers.write_json(
            temp_dir / f"poolB_{no_of_addr}_registration_metadata.json", pool_metadata
        )

        pool_data = clusterlib.PoolData(
            pool_name=f"poolB_{no_of_addr}",
            pool_pledge=4567,
            pool_cost=3,
            pool_margin=0.01,
            pool_metadata_url="https://www.where_metadata_file_is_located.com",
            pool_metadata_hash=cluster.gen_pool_metadata_hash(pool_metadata_file),
        )

        pool_data_updated = pool_data._replace(pool_pledge=1, pool_cost=1_000_000, pool_margin=0.9)

        # create pool owners
        pool_owners = _create_pool_owners(
            cluster_obj=cluster, temp_template=temp_template, no_of_addr=no_of_addr,
        )

        # fund source address
        helpers.fund_from_faucet(
            pool_owners[0].payment,
            cluster_obj=cluster,
            faucet_data=addrs_data_session["user1"],
            amount=900_000_000,
            request=request,
        )

        # register pool and delegate stake address
        pool_artifacts = _create_register_pool_tx_delegate_stake_tx(
            cluster_obj=cluster,
            pool_owners=pool_owners,
            temp_template=temp_template,
            pool_data=pool_data,
        )

        # update the pool parameters by resubmitting the pool registration certificate
        cluster.register_stake_pool(
            pool_data=pool_data_updated,
            pool_owners=pool_owners,
            node_vrf_vkey_file=pool_artifacts.vrf_key_pair.vkey_file,
            node_cold_key_pair=pool_artifacts.cold_key_pair_and_counter,
            deposit=0,  # no additional deposit, the pool is already registered
        )
        cluster.wait_for_new_epoch()

        # check that the pool has it's original ID after updating the parameters
        new_stake_pool_id = cluster.get_stake_pool_id(
            pool_artifacts.cold_key_pair_and_counter.vkey_file
        )
        assert (
            pool_artifacts.stake_pool_id == new_stake_pool_id
        ), "New pool ID was generated after updating the pool parameters"

        # check that the pool parameters were correctly updated on chain
        updated_pool_ledger_state = (
            cluster.get_registered_stake_pools_ledger_state().get(pool_artifacts.stake_pool_id)
            or {}
        )
        assert not helpers.check_pool_data(updated_pool_ledger_state, pool_data_updated)


@pytest.mark.clean_cluster
@pytest.mark.usefixtures("temp_dir", "update_pool_cost")
class TestPoolCost:
    @pytest.fixture(scope="class")
    def pool_owners(
        self, cluster_class: clusterlib.ClusterLib, addrs_data_class: dict, request: FixtureRequest
    ):
        """Create class scoped pool owners."""
        rand_str = clusterlib.get_rand_str()
        temp_template = f"test_pool_cost_class_{rand_str}"

        pool_owners = _create_pool_owners(
            cluster_obj=cluster_class, temp_template=temp_template, no_of_addr=1,
        )

        # fund source address
        helpers.fund_from_faucet(
            pool_owners[0].payment,
            cluster_obj=cluster_class,
            faucet_data=addrs_data_class["user1"],
            amount=900_000_000,
            request=request,
        )

        return pool_owners

    @hypothesis.given(pool_cost=st.integers(max_value=4999))  # minPoolCost is now 5000
    @hypothesis.settings(deadline=None)
    def test_stake_pool_low_cost(
        self,
        cluster_class: clusterlib.ClusterLib,
        pool_owners: List[clusterlib.PoolOwner],
        pool_cost: int,
    ):
        """Try to create and register a stake pool with pool cost lower than 'minPoolCost'."""
        cluster = cluster_class
        rand_str = clusterlib.get_rand_str()
        temp_template = f"test_stake_pool_low_cost_{rand_str}"

        pool_data = clusterlib.PoolData(
            pool_name=f"pool_{rand_str}", pool_pledge=12345, pool_cost=pool_cost, pool_margin=0.123,
        )

        # register pool and delegate stake address, expect failure
        with pytest.raises(clusterlib.CLIError) as excinfo:
            _create_register_pool_delegate_stake_tx(
                cluster_obj=cluster,
                pool_owners=pool_owners,
                temp_template=temp_template,
                pool_data=pool_data,
            )

        # check that it failed in an expected way
        expected_msg = "--pool-cost: Failed reading" if pool_cost < 0 else "StakePoolCostTooLowPOOL"
        assert expected_msg in str(excinfo.value)

    @pytest.mark.parametrize("pool_cost", [5000, 9999999])
    def test_stake_pool_cost(
        self,
        cluster_class: clusterlib.ClusterLib,
        addrs_data_class: dict,
        pool_cost: int,
        request: FixtureRequest,
    ):
        """Create and register a stake pool with pool cost >= 'minPoolCost'."""
        cluster = cluster_class
        rand_str = clusterlib.get_rand_str()
        temp_template = f"test_stake_pool_cost_{rand_str}"

        pool_data = clusterlib.PoolData(
            pool_name=f"pool_{rand_str}", pool_pledge=12345, pool_cost=pool_cost, pool_margin=0.123,
        )

        # create pool owners
        pool_owners = _create_pool_owners(
            cluster_obj=cluster, temp_template=temp_template, no_of_addr=1,
        )

        # fund source address
        helpers.fund_from_faucet(
            pool_owners[0].payment,
            cluster_obj=cluster,
            faucet_data=addrs_data_class["user1"],
            amount=900_000_000,
            request=request,
        )

        # register pool and delegate stake address
        _create_register_pool_delegate_stake_tx(
            cluster_obj=cluster,
            pool_owners=pool_owners,
            temp_template=temp_template,
            pool_data=pool_data,
        )
