// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title Kryvora Campaign Demo
/// @notice Minimal contract used to satisfy the Kryvora "Deploy a Contract" + "Verify on Blockscout" quests.
contract KryvoraDemo {
    string public constant NAME = "Kryvora Campaign Demo";
    string public constant VERSION = "1.0.0";
    address public immutable owner;
    uint256 public counter;
    mapping(address => uint256) public bumps;

    event Bumped(address indexed who, uint256 value);

    constructor() {
        owner = msg.sender;
    }

    function bump() external {
        counter += 1;
        bumps[msg.sender] += 1;
        emit Bumped(msg.sender, counter);
    }
}
