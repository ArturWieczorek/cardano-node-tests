"""Tests for protocol state and protocol parameters."""
import json
import logging

import allure
import pytest
from cardano_clusterlib import clusterlib

from cardano_node_tests.tests import common
from cardano_node_tests.utils import helpers

LOGGER = logging.getLogger(__name__)


PROTOCOL_STATE_KEYS = ("csLabNonce", "csProtocol", "csTickn")
PROTOCOL_PARAM_KEYS = (
    "collateralPercentage",
    "costModels",
    "decentralization",
    "executionUnitPrices",
    "extraPraosEntropy",
    "maxBlockBodySize",
    "maxBlockExecutionUnits",
    "maxBlockHeaderSize",
    "maxCollateralInputs",
    "maxTxExecutionUnits",
    "maxTxSize",
    "maxValueSize",
    "minPoolCost",
    "minUTxOValue",
    "monetaryExpansion",
    "poolPledgeInfluence",
    "poolRetireMaxEpoch",
    "protocolVersion",
    "stakeAddressDeposit",
    "stakePoolDeposit",
    "stakePoolTargetNum",
    "treasuryCut",
    "txFeeFixed",
    "txFeePerByte",
    "utxoCostPerWord",
)


@pytest.mark.skipif(not common.SAME_ERAS, reason=common.ERAS_SKIP_MSG)
@pytest.mark.testnets
@pytest.mark.smoke
class TestProtocol:
    """Basic tests for protocol."""

    @allure.link(helpers.get_vcs_link())
    def test_protocol_state_keys(self, cluster: clusterlib.ClusterLib):
        """Check output of `query protocol-state`."""
        common.get_test_id(cluster)

        # TODO: the query is currently broken
        query_currently_broken = False
        try:
            protocol_state = cluster.get_protocol_state()
        except clusterlib.CLIError as err:
            if "currentlyBroken" not in str(err):
                raise
            query_currently_broken = True
        if query_currently_broken:
            pytest.xfail("`query protocol-state` is currently broken - cardano-node issue #3883")

        assert tuple(sorted(protocol_state)) == PROTOCOL_STATE_KEYS

    @allure.link(helpers.get_vcs_link())
    @pytest.mark.xfail
    def test_protocol_state_outfile(self, cluster: clusterlib.ClusterLib):
        """Check output file produced by `query protocol-state`."""
        common.get_test_id(cluster)
        protocol_state: dict = json.loads(
            cluster.query_cli(["protocol-state", "--out-file", "/dev/stdout"])
        )
        assert tuple(sorted(protocol_state)) == PROTOCOL_STATE_KEYS

    @allure.link(helpers.get_vcs_link())
    def test_protocol_params(self, cluster: clusterlib.ClusterLib):
        """Check output of `query protocol-parameters`."""
        common.get_test_id(cluster)
        protocol_params = cluster.get_protocol_params()
        assert tuple(sorted(protocol_params.keys())) == PROTOCOL_PARAM_KEYS
