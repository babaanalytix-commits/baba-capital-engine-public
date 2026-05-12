# Deploying `BabaCapitalEngineMarker.sol` to Base

A 60-second deploy via [Remix](https://remix.ethereum.org) — no toolchain
install required.

**Cost**: ~$0.01 of ETH on Base mainnet (Base gas is essentially free).

---

## Prerequisites

- MetaMask (or any EVM wallet) connected to **Base Mainnet** (chainId 8453)
- A small amount of ETH on Base in your wallet (0.001 ETH is more than enough)
- Your wallet should be the address you want recorded as the immutable
  `deployer`

## Step-by-step

### 1. Open Remix
Go to https://remix.ethereum.org

### 2. Create the file
- In the left sidebar, click the **File Explorer** icon
- Click **New File** → name it `BabaCapitalEngineMarker.sol`
- Paste the contents of [`BabaCapitalEngineMarker.sol`](./BabaCapitalEngineMarker.sol)

### 3. Compile
- Click the **Solidity Compiler** icon (left sidebar, second from top)
- Compiler version: **0.8.20** or any 0.8.x
- Click **Compile BabaCapitalEngineMarker.sol**
- Should show a green checkmark, no warnings

### 4. Connect MetaMask
- Click the **Deploy & Run Transactions** icon (third from top)
- **Environment** dropdown: select **Injected Provider — MetaMask**
- MetaMask will prompt to connect — approve
- Confirm your MetaMask network shows **Base Mainnet** (top-right of MetaMask)
- The Remix UI should now show your wallet address under "Account"

### 5. Deploy
- **Contract** dropdown: select `BabaCapitalEngineMarker - contracts/...`
- Click the orange **Deploy** button
- MetaMask pops up — confirm the transaction
- Wait ~3 seconds for the Base block

### 6. Capture the address
After deploy succeeds:
- Look at the bottom of Remix — under "Deployed Contracts" you'll see your
  contract address (e.g. `0xAbCd...1234`)
- Copy it
- Update [`../README.md`](../README.md) — replace the placeholder
  `Base mainnet: 0x...  (pending deploy)` with your real address
- Commit + push

### 7. (Optional) Verify on Basescan
- Visit `https://basescan.org/address/<your-contract-address>`
- Click the **Contract** tab → **Verify and Publish**
- Compiler: 0.8.20 (or whichever you used)
- License: MIT
- Paste the source code
- Submit — verification is instant for tiny contracts like this

Once verified, anyone can call `metadata()` directly from Basescan and see
the project marker in a human-readable form.

---

## Why this contract exists

Talent Protocol's Builder Score considers Base on-chain activity — wallet
age, transaction count, **contracts deployed by you on Base**. This marker
provides one such contract: simple enough to deploy in a minute, with no
attack surface (no external calls, no payable functions, no admin paths),
but materially boosting the on-chain activity score for the deployer.

It also serves as a verifiable bridge between this GitHub repository and a
Base address. The `repository` field is immutable; the `Deployed` event is
indexed by deployer; anyone querying the contract can confirm "this address
authored the BABA Capital Engine repo".

The contract holds no funds and provides no functions beyond reads. It is
intentionally minimal.
