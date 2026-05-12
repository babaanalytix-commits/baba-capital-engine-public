// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title  BabaCapitalEngineMarker
 * @author Yomi Oguntona
 * @notice Minimal on-chain authorship marker for the BABA Capital Engine
 *         project. Deployed to Base mainnet. Stores immutable project
 *         metadata + the deployer's address + a deployment timestamp.
 *
 *         No mutable state. No admin functions. No funds held. No external
 *         calls. The contract exists to bind the GitHub repository to a
 *         verifiable Base on-chain identity, contribute on-chain activity
 *         to Builder Score / Talent Protocol, and serve as a public
 *         reference point for the engine's published architecture.
 *
 *         This is a marker, not a product. The strategy logic lives in a
 *         private codebase; this contract advertises its existence.
 */
contract BabaCapitalEngineMarker {
    /// Human-readable project name.
    string public constant project = "BABA Capital Engine";

    /// Tagline describing the project's scope.
    string public constant tagline =
        "Multi-venue crypto perpetuals + funding-rate arbitrage + prediction markets";

    /// Canonical public repository URL.
    string public constant repository =
        "https://github.com/babaanalytix-commits/baba-capital-engine-public";

    /// The address that deployed this marker. Immutable after construction.
    address public immutable deployer;

    /// Block timestamp at which the marker was deployed. Immutable.
    uint256 public immutable deployedAt;

    /// Emitted once at construction. Indexed deployer for easy filtering.
    event Deployed(address indexed deployer, uint256 timestamp, string repository);

    constructor() {
        deployer = msg.sender;
        deployedAt = block.timestamp;
        emit Deployed(msg.sender, block.timestamp, repository);
    }

    /// Returns all marker metadata in one call (gas-cheap view).
    function metadata()
        external
        view
        returns (
            string memory _project,
            string memory _tagline,
            string memory _repository,
            address _deployer,
            uint256 _deployedAt
        )
    {
        return (project, tagline, repository, deployer, deployedAt);
    }
}
