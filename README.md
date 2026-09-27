# Kryvora Network campaign bot

Full-auto quest farmer for the **Kryvora Network** L2 points campaign
(chain `73829164`, OP-Stack settling to Sepolia). Reconciled live against the
chain + portal on **2026-09-27**.

```
PTS -> $KRV         100 PTS = 1 $KRV   (published ratio, unchanged)
Free route          ~10,150 PTS        (all no-cost quests)
Daily               +150 PTS/day       (check-in 50 + swap 100)
Paid route          node key 10 USDC   -> +120,000 PTS + 2x multiplier
```

---

## 1. How the campaign actually works (reverse-engineered)

### Auth — EIP-191 wallet login, no browser needed

```
POST /api/auth/wallet/nonce   {address, referralCode?}  -> {message}
     personal_sign(message, address)                     # EIP-191, utf-8
POST /api/auth/wallet/verify  {address, signature}      -> {token, user}
     -> Bearer <token> on every later call
```

The nonce message is a plain-text block (Origin / Chain ID / Address / Nonce /
Issued At / Expiration, 5-minute TTL). No SIWE parsing is required.

### Quest completion

```
POST /api/portal/quests/{id}/verify   {txHash?, chainIdHex?, tweetUrl?, proofImageBase64?}
POST /api/portal/quests/{id}/claim    {}
```

The server **verifies on-chain state itself** for most quests:

| Verify style | Quests |
|---|---|
| auto-detected on-chain activity | `quest-l2-tx`, `quest-faucet` |
| pure client-side | `quest-wallet`, `quest-add-network` (`chainIdHex`), `quest-daily-gm` |
| needs `txHash` | `quest-bridge`, `quest-deploy`, `quest-verify-contract`, `quest-lending-supply`, `quest-lending-borrow`, `quest-provide-liquidity`, `quest-daily-swap`, `quest-explore-blockscout`, `quest-nuke-*-mint` |
| needs `tweetUrl` | `quest-quote-x`, `quest-tokenomics-x`, `quest-star-github-node` (GitHub username goes in this field) |
| needs `proofImageBase64` | all `quest-repost-x` |
| needs real OAuth | `quest-connect-x`, `quest-follow-x`, `quest-connect-discord`, `quest-join-discord` |

### Contracts (all verified with `eth_getCode`)

| Thing | Address |
|---|---|
| Router (Uniswap-V2 fork) | `0x40E74982f63406e920d3384c4b9295085f72fE1C` |
| Factory | `0x8E9828F8843B24180Eb74e5475394a46822978fC` |
| Router WETH (native pairs) | `0x44f12d0487Cd2efE2C572347578fc253e9B52E62` |
| KALPHA / KBETA | `0x8956e3f2663E033EdbC9656b7509D2657e34db00` / `0x60624B57cabD13d719f3A1bac038D43336eb9d4d` |
| Lending pool | `0x40AdddA3e0295E29045D96FB8503e8cBF3E272d2` |
| Lending WETH / USDC (both have a public `faucet(address,uint256)`) | `0x9cb7d02B7eaa504672b636715da7E46EfA24E269` / `0xB16179D5F2B2898c43cE77B520776A912d838ADC` |
| NFT Genesis / Verdant / Cyan | `0x4d94D63f…` / `0x150E1226…` / `0x8691276a…` — **all SOLD OUT** |
| L1 StandardBridge (Sepolia 11155111) | `0x803bc3291b79c118eb6a8431a42b9946bbc4ee36` (`bridgeETHTo`, v2.8.2) |
| Genesis Node Key (Arbitrum One) | `0xBCf2cD12D1D37578fA7C69805fE77788e866BE1a` |

KALPHA / KBETA have **no faucet** — they only exist through router swaps. The
lending WETH / USDC mocks *do* have a callable `faucet()`, which is how the
supply/borrow quests are reachable at zero cost.

### Faucet

`POST https://faucet-testnet.kryvora.network/api/faucet` `{address, turnstileToken}` —
Cloudflare Turnstile sitekey `0x4AAAAAAEkL7cideZI76RLn`, 0.001 ETH per claim,
**rate-limited per IP**. 2captcha solves it in ~10 s.

---

## 2. What is automatable

**Bot does it end-to-end (no human):**
- connect wallet, add network, daily check-in — `+300 PTS`
- L2 faucet claim (Turnstile via 2captcha) — `+250 PTS`
- L2 transaction (self-transfer) — `+300 PTS`
- swap ETH -> KALPHA + `quest-daily-swap` — `+400 PTS`
- addLiquidityETH — `+500 PTS`
- lending supply WETH + borrow USDC — `+850 PTS`
- deploy contract + verify on Blockscout — `+1,100 PTS`
- Blockscout explore — `+350 PTS`
- bridge Sepolia -> Kryvora (needs a funded Sepolia key) — `+400 PTS`

**Needs a human once:**
- X / Discord OAuth + the follow / repost / quote proof screenshots — `+1,750 PTS`
- GitHub star (`quest-star-github-node`) — username, not a tx

**Dead / unprofitable:**
- the three NFT mints — **sold out** (10,000/10,000 each), `+1,500 PTS` unreachable
- Genesis Node Key — real 10 USDC on Arbitrum One, left off by default

### Verified end-to-end result

**Full free route, every automatable quest, one wallet:**

```
wallet A   4,150 PTS   rank #46,205   11 quests   (incl. bridge)
```

Earlier pair, faucet-funded only (no Sepolia), hit `3,750 PTS` each with 10 quests.

`4,150 PTS = 41.5 $KRV`. Daily check-in + swap adds `+150 PTS/day` on repeat runs.
The only PTS left on the table are the manual ones: X/Discord OAuth + proof
screenshots (`1,750`) and the GitHub star, plus the dead NFT mints (`1,500`).

On-chain actions confirmed working: faucet, self-transfer, `swapExactETHForTokens`,
`addLiquidityETH`, WETH `faucet()` -> `approve` -> `supply`, USDC `faucet()` -> `borrow`
(75 USD capacity off 0.05 WETH collateral), `forge create`, Blockscout verification,
Sepolia -> Kryvora `depositETH` bridge, and quest verify+claim for every one of them.

---

## 3. Install & run

Foundry must be on PATH for the deploy/verify step (`forge`, `cast`).

```bash
cd kryvora-bot
cp .env.example .env

python3 kryvora_bot.py --gen 3      # print 3 fresh wallets -> paste WALLETS= into .env
python3 kryvora_bot.py --dry        # auth + free quests only, zero chain writes
python3 kryvora_bot.py              # full pipeline
python3 kryvora_bot.py --status     # points / rank / claimable per wallet
python3 kryvora_bot.py --only faucet,l2tx,swap,lp,lending,deploy,explore
```

`.env` essentials:

```env
WALLETS=[{"pk":"0x...","evm_address":"0x..."}]
TWOCAPTCHA_KEY=...
SEPOLIA_PRIVATE_KEY=          # only for the bridge quest
AUTO_LOOP=false               # true = rerun the whole cycle forever
DELAY_AFTER_CYCLE=86400
```

### Funding note (important)

The L2 faucet is **per-IP rate limited**. One IP cannot fund many wallets.
Options, in order of practicality:

1. run the bot from a residential / mobile IP (the limit is IP, not address)
2. one HTTP proxy per wallet — `python3 kryvora_bot.py --proxy http://user:pass@host:port`
3. bridge Sepolia ETH instead of using the faucet (also earns `+400 PTS`)

`faucet_retry.py <address> [interval] [attempts]` keeps retrying the faucet
until the IP window reopens (each accepted attempt costs one 2captcha solve).

---

## 4. Files

| File | Purpose |
|---|---|
| `kryvora_bot.py` | everything: portal client, captcha, chain actions, orchestrator |
| `faucet_retry.py` | standalone faucet retry loop |
| `contracts/KryvoraDemo.sol` | the contract deployed for the deploy + verify quests |
| `foundry.toml` | solc 0.8.24, optimizer on (must match for verification) |
| `.env` | wallets, captcha key, toggles |
| `README.md` | this file |

---

## 5. Pitfalls learned

- **The Kryvora bridge is the *legacy* OP-Stack bridge** — it exposes
  `depositETH(uint32,bytes)` (`0xb1a1a882`, the `depositSelector` in `dapps…/config.js`),
  **not** `bridgeETHTo`. Calling `bridgeETHTo` reverts.
- **OP `ResourceMetering` makes the bridge gas need far higher than `eth_estimateGas`
  reports.** At a 650k limit the live call reverts; 900k passes. Probe with
  `eth_estimateGas`, then take ~1.8x and never go below 1M — a wrong guess costs the
  whole gas budget on a reverted tx and can drain a thinly funded L1 key.
- The portal only accepts the bridge deposit after ~6 L1 confirmations, and it looks for
  the `ETHDepositInitiated` topic `0x35d79ab8…` with `to == sender`.
- **Cloudflare Turnstile tokens are bound to the solving IP.** A
  `TurnstileTaskProxyless` solve submitted from a different IP gets rejected. Use
  the `TurnstileTask` type *with* proxy credentials so 2captcha solves from the
  same exit IP that submits the faucet POST.
- **The Kryvora faucet is limited per IP *and* per address, 0.001 ETH / 24 h.**
  One IP cannot fund many wallets — rotate the proxy session per wallet
  (`--fund` does this). A rate-limited attempt still burns a captcha solve
  because the check runs *after* captcha validation, so don't hammer it.
- **This web3 build (6.20.0) rejects human-readable ABIs** — `w3.eth.contract(abi=["function foo()"])`
  raises `'abi' is not a list of dictionaries`. Build JSON ABI dicts (`_fn()` helper in the bot).
- **Nonces must come from `'pending'`.** `get_transaction_count(addr)` defaults to
  `'latest'` and lags behind just-broadcast txs, producing `nonce too low`.
  `approve` must also be *confirmed* before the next tx, not just broadcast.
- **`forge create --json` pretty-prints.** Parsing only the last stdout line gives `}`;
  `json.loads(whole stdout)` or regex for `"deployedTo"`.
- **`forge verify-contract` prints level=info lines as `ok`** and may still say
  "Verification is still pending" while the contract is already verified. Detect on
  `successfully verified` / `pass - verified` (case-insensitive), not the exit status.
- **The NFT contracts' names are scrambled vs the quest ids** — `0x4d94…` is
  "Kryvora Nuke Genesis", `0x150E…` is "Verdant Core". Match by name, not position.
- **`quest-star-github-node` reuses the `tweetUrl` field** for the GitHub username.
- **`quest-explore-blockscout` requires a tx hash** even though the UI lets you submit empty.
- **`totalMinted()` is the source of truth** for NFT availability — check before minting.
- Router WETH (`0x44f1…`) and lending WETH (`0x9cb7…`) are **different tokens**.
- The lending reserve reports `18` decimals for USDC even though the USDC token has 6 —
  the pool does its own scaling; pass raw token units (1e6 for USDC).
- **ETH budget is tight**: 0.001 ETH per wallet has to cover ~10 txs. Keep the gas
  floor low (0.2 gwei) and clamp value-bearing amounts against the live balance
  (`spendable()`), or `addLiquidityETH` fails with `insufficient funds for gas * price + value`.
- The `quest-faucet` / `quest-l2-tx` verifiers are retried by the UI at 0/5/10/20 s
  because the portal indexes the chain asynchronously — do the same before giving up.
