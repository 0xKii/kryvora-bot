#!/usr/bin/env python3
"""
Kryvora Network campaign bot  —  full-auto testnet quest farmer
==============================================================

Chain   : Kryvora L2 (OP-Stack)  chainId 73829164
Portal  : https://tasks.kryvora.network
Faucet  : https://faucet-testnet.kryvora.network  (Cloudflare Turnstile)
DApps   : https://dapps.kryvora.network           (Uniswap-V2 fork swap / LP)
Lending : https://lending.kryvora.network         (Compound-style pool)
NFT     : https://nft.kryvora.network             (SOLD OUT as of 2026-09-27)

Auth    : EIP-191 personal_sign over a server nonce -> Bearer JWT
Quests  : POST /api/portal/quests/{id}/verify  then  /claim

Usage
-----
    python3 kryvora_bot.py                 # run pipeline (from .env)
    python3 kryvora_bot.py --gen 3         # generate 3 fresh wallets into .env
    python3 kryvora_bot.py --status        # print portal status for every wallet
    python3 kryvora_bot.py --dry           # auth + free quests only, no chain writes
    python3 kryvora_bot.py --only faucet,swap,lp,lending,deploy,bridge
"""

import argparse
import json
import os
import random
import re
import sys
import time
import traceback
from datetime import datetime, timezone

import requests
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import keccak

try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:  # dotenv optional
    pass

Account.enable_unaudited_hdwallet_features()

# --------------------------------------------------------------------------- #
#  Chain constants (all verified on-chain 2026-09-27)
# --------------------------------------------------------------------------- #
CHAIN_ID = 73829164
CHAIN_ID_HEX = "0x4668b2c"
RPC = os.getenv("KRYVORA_RPC", "https://rpc-testnet.kryvora.network")
EXPLORER = "https://explorer-testnet.kryvora.network"
PORTAL = "https://tasks.kryvora.network"
FAUCET = "https://faucet-testnet.kryvora.network"
TURNSTILE_SITEKEY = "0x4AAAAAAEkL7cideZI76RLn"

# Sepolia side of the OP-Stack bridge
SEPOLIA_RPC = os.getenv("SEPOLIA_RPC", "https://ethereum-sepolia-rpc.publicnode.com")
SEPOLIA_CHAIN_ID = 11155111
L1_STANDARD_BRIDGE = "0x803bc3291b79c118eb6a8431a42b9946bbc4ee36"
BRIDGE_MIN_GAS_LIMIT = 200000

# DEX (Uniswap-V2 fork)
ROUTER = "0x40E74982f63406e920d3384c4b9295085f72fE1C"
FACTORY = "0x8E9828F8843B24180Eb74e5475394a46822978fC"
ROUTER_WETH = "0x44f12d0487Cd2efE2C572347578fc253e9B52E62"  # wrapped native used by the router pairs
KALPHA = "0x8956e3f2663E033EdbC9656b7509D2657e34db00"
KBETA = "0x60624B57cabD13d719f3A1bac038D43336eb9d4d"

# Lending (Compound-style, own mock WETH/USDC with public faucet())
LENDING_POOL = "0x40AdddA3e0295E29045D96FB8503e8cBF3E272d2"
LENDING_WETH = "0x9cb7d02B7eaa504672b636715da7E46EfA24E269"
LENDING_USDC = "0xB16179D5F2B2898c43cE77B520776A912d838ADC"

# NFTs — all three reported totalMinted == MAX_SUPPLY (sold out)
NFT_GENESIS = "0x4d94D63fB58b4Cef7f1F1A5275E0DBeA2fdD81c0"  # "Kryvora Nuke Genesis"
NFT_VERDANT = "0x150E1226c8F6a46A3D00BD50d8F43420432aB940"  # "Kryvora Nuke: Verdant Core"
NFT_CYAN = "0x8691276a06Dc88D6b30271813D58B8A6E2cB7C0c"    # "Kryvora Nuke: Cyan Reactor"

# Arbitrum One — Genesis Node Key (paid, real USDC)
ARB_RPC = os.getenv("ARB_RPC", "https://arb1.arbitrum.io/rpc")
NODE_KEY_CONTRACT = os.getenv("NODE_KEY_CONTRACT", "")

# --------------------------------------------------------------------------- #
#  ABIs  (this web3 build rejects human-readable ABIs, so build JSON dicts)
# --------------------------------------------------------------------------- #
def _fn(name, ins, outs=None, mut="nonpayable"):
    return {
        "type": "function",
        "name": name,
        "stateMutability": mut,
        "inputs": [{"name": "", "type": t} for t in ins],
        "outputs": [{"name": "", "type": t} for t in (outs or [])],
    }


ERC20_ABI = [
    _fn("balanceOf", ["address"], ["uint256"], "view"),
    _fn("totalSupply", [], ["uint256"], "view"),
    _fn("decimals", [], ["uint8"], "view"),
    _fn("symbol", [], ["string"], "view"),
    _fn("approve", ["address", "uint256"], ["bool"]),
    _fn("allowance", ["address", "address"], ["uint256"], "view"),
    _fn("transfer", ["address", "uint256"], ["bool"]),
    _fn("faucet", ["address", "uint256"]),
]

ROUTER_ABI = [
    _fn("getAmountsOut", ["uint256", "address[]"], ["uint256[]"], "view"),
    _fn("swapExactETHForTokens", ["uint256", "address[]", "address", "uint256"], ["uint256[]"], "payable"),
    _fn("swapExactTokensForETH", ["uint256", "uint256", "address[]", "address", "uint256"], ["uint256[]"]),
    _fn("swapExactTokensForTokens", ["uint256", "uint256", "address[]", "address", "uint256"], ["uint256[]"]),
    _fn("addLiquidityETH", ["address", "uint256", "uint256", "uint256", "address", "uint256"],
        ["uint256", "uint256", "uint256"], "payable"),
]

POOL_ABI = [
    _fn("supply", ["address", "uint256"]),
    _fn("withdraw", ["address", "uint256"]),
    _fn("borrow", ["address", "uint256"]),
    _fn("repay", ["address", "uint256"]),
    _fn("getUserAccountData", ["address"], ["uint256", "uint256", "uint256", "uint256"], "view"),
    _fn("getUserBalance", ["address", "address"], ["uint256", "uint256"], "view"),
    _fn("reserves", ["address"],
        ["bool", "uint256", "uint256", "uint256", "uint256", "uint256", "uint8", "uint256", "uint256", "uint256", "uint256", "uint256"],
        "view"),
]

NFT_ABI = [
    _fn("mint", ["uint256"], mut="payable"),
    _fn("NAME", [], ["string"], "view"),
    _fn("SYMBOL", [], ["string"], "view"),
    _fn("MAX_SUPPLY", [], ["uint256"], "view"),
    _fn("totalMinted", [], ["uint256"], "view"),
    _fn("mintPrice", [], ["uint256"], "view"),
    _fn("TOKEN_ID", [], ["uint256"], "view"),
    _fn("balanceOf", ["address", "uint256"], ["uint256"], "view"),
]

# The bridge is called with raw calldata (see step_bridge) — no ABI object needed.

# --------------------------------------------------------------------------- #
#  Small helpers
# --------------------------------------------------------------------------- #
def ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def log(msg: str, tag: str = "INFO") -> None:
    icons = {"INFO": "·", "OK": "✅", "ERR": "❌", "WARN": "⚠️", "TASK": "▶", "STEP": "•", "SKIP": "⏭"}
    print(f"[{ts()}] {icons.get(tag, '·')} {msg}", flush=True)


def log_acct(wallet: str, msg: str, tag: str = "INFO") -> None:
    icons = {"INFO": "·", "OK": "✅", "ERR": "❌", "WARN": "⚠️", "TASK": "▶", "STEP": "•", "SKIP": "⏭"}
    print(f"[{ts()}] {icons.get(tag, '·')} [{wallet[:6]}…{wallet[-4:]}] {msg}", flush=True)


def env_bool(key: str, default: bool = False) -> bool:
    v = os.getenv(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except Exception:
        return default


def env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except Exception:
        return default


def redact(proxy_url: str) -> str:
    """Hide credentials when logging a proxy."""
    try:
        from urllib.parse import urlparse, urlunparse

        u = urlparse(proxy_url if "://" in proxy_url else "http://" + proxy_url)
        net = f"{u.hostname}:{u.port}" if u.port else (u.hostname or proxy_url)
        return f"{u.scheme}://***@{net}"
    except Exception:
        return "***"


def rotate_proxy(line: str, n: int, country: str = "us") -> str:
    """Return the proxy with a fresh sticky session (new exit IP).

    Enabled with PROXY_STICKY=true. Assumes the gateway accepts a sticky suffix
    appended to the username, e.g. DataImpulse:
        user__cr.us;sessid.sX;sesstime.15
    Proxies that rotate per connection need no session id — leave PROXY_STICKY off.
    """
    if not line or not env_bool("PROXY_STICKY", False):
        return line
    try:
        from urllib.parse import urlparse, unquote

        u = urlparse(line if "://" in line else "http://" + line)
        if not u.username:
            return line
        base_user = unquote(u.username).split("__", 1)[0]
        sess = f"s{int(time.time())}{n}{random.randint(10, 99)}"
        user = f"{base_user}__cr.{country};sessid.{sess};sesstime.15"
        pwd = unquote(u.password or "")
        return f"http://{user}:{pwd}@{u.hostname}:{u.port or 823}"
    except Exception:
        return line


# --------------------------------------------------------------------------- #
#  Turnstile solver (2captcha API v2)
# --------------------------------------------------------------------------- #
class CaptchaSolver:
    def __init__(self, api_key: str, proxy: str | None = None):
        self.key = api_key
        self.proxy = proxy

    def solve_turnstile(self, page_url: str, sitekey: str, timeout: int = 180) -> str | None:
        if not self.key:
            return None
        task = {
            "type": "TurnstileTaskProxyless",
            "websiteURL": page_url,
            "websiteKey": sitekey,
            "action": "managed",
        }
        if self.proxy:
            # solve from the SAME IP that will submit the form — Cloudflare binds
            # the Turnstile token to the solving IP, so proxyless tokens get 403'd
            from urllib.parse import urlparse

            u = urlparse(self.proxy)
            task = {
                "type": "TurnstileTask",
                "websiteURL": page_url,
                "websiteKey": sitekey,
                "action": "managed",
                "proxyType": u.scheme or "http",
                "proxyAddress": u.hostname,
                "proxyPort": u.port,
                "proxyLogin": u.username or "",
                "proxyPassword": u.password or "",
            }
        try:
            r = requests.post(
                "https://api.2captcha.com/createTask",
                json={"clientKey": self.key, "task": task},
                timeout=30,
            )
            d = r.json()
            if d.get("errorId"):
                log(f"2captcha createTask error: {d}", "ERR")
                return None
            task_id = d["taskId"]
        except Exception as e:
            log(f"2captcha submit failed: {e}", "ERR")
            return None

        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(5)
            try:
                r = requests.post(
                    "https://api.2captcha.com/getTaskResult",
                    json={"clientKey": self.key, "taskId": task_id},
                    timeout=30,
                )
                d = r.json()
                if d.get("status") == "ready":
                    return d["solution"]["token"]
                if d.get("errorId"):
                    log(f"2captcha solve error: {d.get('errorCode')}", "ERR")
                    return None
            except Exception:
                continue
        log("2captcha timeout", "ERR")
        return None


# --------------------------------------------------------------------------- #
#  Portal client
# --------------------------------------------------------------------------- #
class Portal:
    def __init__(self, proxy: str | None = None):
        self.s = requests.Session()
        self.s.headers.update(
            {
                "Content-Type": "application/json",
                "Origin": PORTAL,
                "Referer": PORTAL + "/",
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
                ),
            }
        )
        if proxy:
            self.s.proxies.update({"http": proxy, "https": proxy})
        self.token: str | None = None
        self.user: dict = {}

    # ---- auth ------------------------------------------------------------ #
    def login(self, account, referral: str | None = None) -> dict:
        addr = account.address
        r = self.s.post(
            PORTAL + "/api/auth/wallet/nonce",
            json={"address": addr, "referralCode": referral} if referral else {"address": addr},
            timeout=30,
        )
        r.raise_for_status()
        msg = r.json()["message"]
        sig = Account.sign_message(encode_defunct(text=msg), private_key=account.key).signature.hex()
        if not sig.startswith("0x"):
            sig = "0x" + sig
        r = self.s.post(
            PORTAL + "/api/auth/wallet/verify",
            json={"address": addr, "signature": sig},
            timeout=30,
        )
        r.raise_for_status()
        d = r.json()
        self.token = d["token"]
        self.user = d["user"]
        self.s.headers["Authorization"] = "Bearer " + self.token
        return self.user

    def _req(self, method: str, path: str, **kw):
        timeout = kw.pop("timeout", 30)
        last = None
        for attempt in range(3):
            try:
                r = self.s.request(method, PORTAL + path, timeout=timeout, **kw)
                try:
                    data = r.json()
                except Exception:
                    data = {"error": r.text[:200]}
                return r.status_code, data
            except Exception as e:  # transient proxy/TLS hiccup
                last = e
                time.sleep(2 + attempt * 3)
        return 0, {"error": f"request failed: {last}"}

    def me(self) -> dict:
        _, d = self._req("GET", "/api/portal/me")
        self.user = d.get("user", self.user)
        return self.user

    def quests(self) -> list:
        _, d = self._req("GET", "/api/portal/quests")
        return d.get("quests", [])

    def verify(self, quest_id: str, tx_hash: str | None = None, chain_id_hex: str | None = None,
               tweet_url: str | None = None, proof_b64: str | None = None):
        body = {}
        if tx_hash:
            body["txHash"] = tx_hash
        if chain_id_hex:
            body["chainIdHex"] = chain_id_hex
        if tweet_url:
            body["tweetUrl"] = tweet_url
        if proof_b64:
            body["proofImageBase64"] = proof_b64
        return self._req("POST", f"/api/portal/quests/{quest_id}/verify", json=body)

    def claim(self, quest_id: str):
        return self._req("POST", f"/api/portal/quests/{quest_id}/claim", json={})

    def claimable(self) -> list:
        u = self.me()
        return u.get("claimableQuestIds", []) or []

    def claim_all(self, exclude=("quest-genesis-node",)) -> int:
        """Claim every quest the portal currently marks claimable. Returns points gained."""
        gained = 0
        before = self.me().get("points", 0)
        for qid in list(self.claimable()):
            if qid in exclude:
                log_acct(self.user.get("address", "?"), f"skip paid quest {qid}", "SKIP")
                continue
            st, d = self.claim(qid)
            if st == 200 and d.get("claimed"):
                gained += d.get("pointsAwarded", 0)
                log_acct(self.user.get("address", "?"), f"claimed {qid} +{d.get('pointsAwarded')}", "OK")
            else:
                log_acct(self.user.get("address", "?"), f"claim {qid} failed: {str(d)[:120]}", "WARN")
            time.sleep(0.6)
        after = self.me().get("points", before)
        return after - before

    def verify_then_claim(self, quest_id: str, tx_hash: str | None = None,
                          chain_id_hex: str | None = None, tweet_url: str | None = None,
                          proof_b64: str | None = None, retries=(0, 5, 10, 20)) -> bool:
        """Run verify (with backoff, for server-side auto-detection) then claim."""
        addr = self.user.get("address", "?")
        last = None
        for dly in retries:
            if dly:
                time.sleep(dly)
            st, d = self.verify(quest_id, tx_hash, chain_id_hex, tweet_url, proof_b64)
            if st == 200 and d.get("verified"):
                st2, d2 = self.claim(quest_id)
                if st2 == 200 and d2.get("claimed"):
                    log_acct(addr, f"{quest_id} +{d2.get('pointsAwarded')} pts", "OK")
                    return True
                last = d2
                break
            last = d
        log_acct(addr, f"{quest_id} not verified: {str(last)[:140]}", "WARN")
        return False


# --------------------------------------------------------------------------- #
#  Main bot
# --------------------------------------------------------------------------- #
class KryvoraBot:
    def __init__(self, private_key: str, cfg: dict):
        from web3 import Web3

        self.Web3 = Web3
        self.pk = private_key if private_key.startswith("0x") else "0x" + private_key
        self.account = Account.from_key(self.pk)
        self.address = self.account.address
        self.cfg = cfg
        self.proxy = cfg.get("proxy") or None
        self.dry = cfg.get("dry", False)

        self.w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 30, "proxies": self._proxies()}))
        # The residential proxy is only needed for the faucet (per-IP limit) and for
        # binding the Turnstile solve to the same exit IP. Portal + RPC go direct
        # unless PORTAL_PROXY is set explicitly — fewer moving parts.
        self.portal = Portal(cfg.get("portal_proxy") or None)
        self.solver = CaptchaSolver(cfg.get("captcha_key", ""), self.proxy)
        self.results: dict[str, str] = {}
        self.user: dict = {}
        self.start_points: int = 0
        self.l1_key = cfg.get("sepolia_pk") or None

    def _proxies(self):
        """RPC proxy — normally direct; residential proxy is reserved for the faucet."""
        p = os.getenv("RPC_PROXY") or None
        return {"http": p, "https": p} if p else None

    # ------------------------------------------------------------------ #
    def _log(self, msg, tag="INFO"):
        log_acct(self.address, msg, tag)

    def gas(self) -> int:
        try:
            gp = int(self.w3.eth.gas_price)
        except Exception:
            gp = self.Web3.to_wei(1, "gwei")
        # small headroom; the network runs ~0.1-0.35 gwei and ETH is scarce here
        return max(int(gp * 1.25), self.Web3.to_wei(0.2, "gwei"))

    def _nonce(self) -> int:
        """Pending nonce — 'latest' lags behind just-broadcast txs and causes 'nonce too low'."""
        try:
            return self.w3.eth.get_transaction_count(self.address, "pending")
        except Exception:
            return self.w3.eth.get_transaction_count(self.address)

    def _broadcast(self, tx: dict, retry: bool = True) -> str | None:
        try:
            signed = self.account.sign_transaction(tx)
            raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
            return self.w3.eth.send_raw_transaction(raw).hex()
        except Exception as e:
            msg = str(e)
            if retry and ("nonce too low" in msg or "already known" in msg or "replacement" in msg):
                time.sleep(3)
                tx = dict(tx, nonce=self._nonce())
                return self._broadcast(tx, retry=False)
            self._log(f"send failed: {msg[:220]}", "ERR")
            return None

    def _raw_tx(self, to: str, data: str = "0x", value: int = 0, gas: int = 21000) -> str | None:
        if self.dry:
            self._log("DRY-RUN: would send raw tx", "SKIP")
            return None
        return self._broadcast({
            "from": self.address,
            "to": to,
            "data": data,
            "value": value,
            "nonce": self._nonce(),
            "gasPrice": self.gas(),
            "chainId": CHAIN_ID,
            "gas": gas,
        })

    def _send(self, fn, value: int = 0, gas_hint: int = 0) -> str | None:
        """Build / sign / send a contract function. Returns tx hash (None on failure)."""
        if self.dry:
            self._log("DRY-RUN: would send tx", "SKIP")
            return None
        try:
            tx = fn.build_transaction(
                {
                    "from": self.address,
                    "value": value,
                    "nonce": self._nonce(),
                    "gasPrice": self.gas(),
                    "chainId": CHAIN_ID,
                }
            )
            try:
                tx["gas"] = int(fn.estimate_gas({"from": self.address, "value": value}) * 1.25)
            except Exception:
                tx["gas"] = gas_hint or 500000
        except Exception as e:
            self._log(f"could not build tx: {str(e)[:200]}", "ERR")
            return None
        return self._broadcast(tx)

    def wait(self, h: str, label: str = "tx", timeout: int = 180) -> dict | None:
        if not h:
            return None
        try:
            rc = self.w3.eth.wait_for_transaction_receipt(h, timeout=timeout)
            ok = rc.get("status") == 1
            self._log(f"{label} {'ok' if ok else 'REVERTED'} {h[:18]}…", "OK" if ok else "ERR")
            return rc if ok else None
        except Exception as e:
            self._log(f"{label} receipt error: {e}", "ERR")
            return None

    def balance(self) -> float:
        try:
            return float(self.w3.from_wei(self.w3.eth.get_balance(self.address), "ether"))
        except Exception:
            return 0.0

    def erc20(self, addr):
        return self.w3.eth.contract(address=self.w3.to_checksum_address(addr), abi=ERC20_ABI)

    def token_balance(self, addr) -> float:
        try:
            c = self.erc20(addr)
            return float(c.functions.balanceOf(self.address).call()) / 10 ** c.functions.decimals().call()
        except Exception:
            return 0.0

    # ================================================================== #
    #  PHASE 1 — portal auth + zero-cost quests
    # ================================================================== #
    def step_auth(self) -> bool:
        self._log("authenticating with portal (EIP-191)", "STEP")
        for attempt in range(3):
            try:
                u = self.portal.login(self.account, self.cfg.get("referral"))
                self.user = u
                self._log(f"logged in · rank #{u.get('rank')} · {u.get('points')} pts", "OK")
                return True
            except Exception as e:
                if attempt == 2:
                    self._log(f"auth failed: {e}", "ERR")
                    return False
                time.sleep(3 + attempt * 4)
        return False

    def step_free_quests(self) -> None:
        self._log("free quests: connect_wallet / add_network / daily check-in", "STEP")
        self.portal.verify_then_claim("quest-wallet", retries=(0,))
        self.portal.verify_then_claim("quest-add-network", chain_id_hex=CHAIN_ID_HEX, retries=(0,))
        self.portal.verify_then_claim("quest-daily-gm", retries=(0, 3))

    # ================================================================== #
    #  PHASE 2 — faucet (Turnstile) + l2 tx detection
    # ================================================================== #
    def step_faucet(self) -> bool:
        self._log("claiming L2 faucet (0.001 ETH, Cloudflare Turnstile)", "STEP")
        token = self.solver.solve_turnstile(FAUCET + "/", TURNSTILE_SITEKEY)
        if not token:
            self._log("could not solve Turnstile — faucet skipped", "WARN")
            return False
        s = requests.Session()
        s.headers.update(
            {
                "Content-Type": "application/json",
                "Origin": FAUCET,
                "Referer": FAUCET + "/",
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140.0 Safari/537.36",
            }
        )
        if self.proxy:
            s.proxies.update({"http": self.proxy, "https": self.proxy})
        try:
            r = s.post(FAUCET + "/api/faucet", json={"address": self.address, "turnstileToken": token}, timeout=45)
            d = r.json()
        except Exception as e:
            self._log(f"faucet request failed: {e}", "ERR")
            return False
        if r.status_code == 200 and d.get("ok"):
            self._log(f"faucet ok tx={str(d.get('tx'))[:18]}… (+{d.get('amount')})", "OK")
            # wait for the payout to land so the portal can detect it
            for _ in range(15):
                if self.balance() > 0:
                    self._log(f"balance credited: {self.balance():.6f} ETH", "OK")
                    return True
                time.sleep(4)
            self._log("faucet payout not visible yet (will still try portal detect)", "WARN")
            return True
        self._log(f"faucet rejected: {str(d)[:140]}", "WARN")
        return False

    def step_l2_tx(self, amount_eth: float = 0.0001) -> str | None:
        """Self-transfer — cheapest possible successful tx to satisfy quest-l2-tx."""
        self._log(f"sending self-transfer of {amount_eth} ETH (l2-tx quest)", "STEP")
        if self.balance() < amount_eth + 0.00005:
            self._log("balance too low for self-transfer", "WARN")
            return None
        h = self._raw_tx(self.address, value=self.w3.to_wei(amount_eth, "ether"), gas=21000)
        if not h or not self.wait(h, "self-transfer"):
            return None
        self.results["l2_tx"] = h
        self.portal.verify_then_claim("quest-l2-tx", retries=(0, 5, 10))
        return h

    def spendable(self, want_eth: float) -> float:
        """Clamp a value-bearing amount so gas + value still fit in the balance."""
        reserve = 0.00008  # ~2 txs of headroom at 0.2-0.35 gwei
        room = max(0.0, self.balance() - reserve)
        if room <= 0:
            return 0.0
        return min(want_eth, round(room * 0.45, 8))

    # ================================================================== #
    #  PHASE 3 — DEX: swap + liquidity
    # ================================================================== #
    def step_swap(self, token_addr: str, amount_eth: float = 0.0004) -> str | None:
        router = self.w3.eth.contract(address=self.w3.to_checksum_address(ROUTER), abi=ROUTER_ABI)
        symbol = self.erc20(token_addr).functions.symbol().call()
        self._log(f"swapping {amount_eth} ETH -> {symbol}", "STEP")
        if self.dry:
            self._log("DRY-RUN: would swap ETH -> " + symbol, "SKIP")
            return None
        if amount_eth <= 0 or self.balance() < amount_eth + 0.00003:
            self._log("balance too low for swap", "WARN")
            return None
        try:
            amount_in = self.w3.to_wei(amount_eth, "ether")
            path = [self.w3.to_checksum_address(ROUTER_WETH), self.w3.to_checksum_address(token_addr)]
            amounts = router.functions.getAmountsOut(amount_in, path).call()
        except Exception as e:
            self._log(f"getAmountsOut failed (pair missing?): {e}", "ERR")
            return None
        deadline = int(time.time()) + 900
        try:
            fn = router.functions.swapExactETHForTokens(
                int(amounts[-1] * 0.90), path, self.address, deadline
            ).build_transaction(
                {
                    "from": self.address,
                    "value": amount_in,
                    "nonce": self.w3.eth.get_transaction_count(self.address),
                    "gasPrice": self.gas(),
                    "chainId": CHAIN_ID,
                    "gas": 350000,
                }
            )
            signed = self.account.sign_transaction(fn)
            raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
            h = self.w3.eth.send_raw_transaction(raw).hex()
        except Exception as e:
            self._log(f"swap failed: {e}", "ERR")
            return None
        rc = self.wait(h, f"swap ETH->{symbol}")
        return h if rc else None

    def step_lp(self, token_addr: str, eth_amount: float = 0.0004) -> str | None:
        router = self.w3.eth.contract(address=self.w3.to_checksum_address(ROUTER), abi=ROUTER_ABI)
        symbol = self.erc20(token_addr).functions.symbol().call()
        tok = self.erc20(token_addr)
        need = 0
        try:
            path = [self.w3.to_checksum_address(ROUTER_WETH), self.w3.to_checksum_address(token_addr)]
            need = router.functions.getAmountsOut(self.w3.to_wei(eth_amount, "ether"), path).call()[-1]
        except Exception as e:
            self._log(f"LP quote failed: {e}", "ERR")
            return None
        have = tok.functions.balanceOf(self.address).call()
        if have < need:
            self._log(f"need {need / 1e18:.6f} {symbol} for LP, have {have / 1e18:.6f} — swapping first", "INFO")
            if not self.step_swap(token_addr, eth_amount):
                return None
        self._log(f"addLiquidityETH {eth_amount} ETH + {symbol}", "STEP")
        if self.dry:
            self._log("DRY-RUN: would addLiquidityETH " + symbol, "SKIP")
            return None
        self._approve(token_addr, ROUTER, need * 2)
        try:
            fn = router.functions.addLiquidityETH(
                self.w3.to_checksum_address(token_addr),
                need,
                int(need * 0.95),
                int(self.w3.to_wei(eth_amount, "ether") * 0.95),
                self.address,
                int(time.time()) + 900,
            ).build_transaction(
                {
                    "from": self.address,
                    "value": self.w3.to_wei(eth_amount, "ether"),
                    "nonce": self.w3.eth.get_transaction_count(self.address),
                    "gasPrice": self.gas(),
                    "chainId": CHAIN_ID,
                    "gas": 500000,
                }
            )
            signed = self.account.sign_transaction(fn)
            raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
            h = self.w3.eth.send_raw_transaction(raw).hex()
        except Exception as e:
            self._log(f"addLiquidity failed: {e}", "ERR")
            return None
        rc = self.wait(h, f"addLiquidityETH {symbol}")
        return h if rc else None

    def _approve(self, token_addr: str, spender: str, amount: int) -> bool:
        try:
            c = self.erc20(token_addr)
            cur = c.functions.allowance(self.address, self.w3.to_checksum_address(spender)).call()
            if cur >= amount:
                return True
            h = self._send(c.functions.approve(self.w3.to_checksum_address(spender), amount))
            if not h:
                return False
            # MUST confirm: the next tx reuses the nonce and the pool needs the allowance to exist
            return self.wait(h, "approve") is not None
        except Exception as e:
            self._log(f"approve failed: {e}", "ERR")
            return False

    # ================================================================== #
    #  PHASE 4 — lending supply / borrow
    # ================================================================== #
    def step_lending(self, collateral_eth: float = 0.05, borrow_usdc: float = 1.0):
        """Supply WETH collateral then borrow USDC. Uses the mock tokens' public faucet()."""
        self._log("lending: token faucets -> supply WETH -> borrow USDC", "STEP")
        pool = self.w3.eth.contract(address=self.w3.to_checksum_address(LENDING_POOL), abi=POOL_ABI)
        weth_raw = int(collateral_eth * 1e18)
        usdc_raw = int(borrow_usdc * 1e6)

        if self.dry:
            self._log("DRY-RUN: would faucet WETH -> approve -> supply -> borrow USDC", "SKIP")
            return

        # --- 1. get WETH collateral, and make sure the faucet tx is MINED -- #
        have = self.weth_balance()
        if have < collateral_eth:
            h = self._send(self.erc20(LENDING_WETH).functions.faucet(self.address, weth_raw))
            if not h or not self.wait(h, "WETH faucet"):
                self._log("WETH faucet failed — aborting lending", "ERR")
                self.results["lending_supply"] = "faucet-failed"
                return
            for _ in range(10):
                if self.weth_balance() >= collateral_eth:
                    break
                time.sleep(3)
        self._log(f"WETH collateral ready: {self.weth_balance():.6f}", "INFO")

        # --- 2. approve + supply ------------------------------------------- #
        if not self._approve(LENDING_WETH, LENDING_POOL, weth_raw):
            self._log("approve failed — aborting lending", "ERR")
            self.results["lending_supply"] = "approve-failed"
            return
        supply_hash = self._send(pool.functions.supply(self.w3.to_checksum_address(LENDING_WETH), weth_raw))
        if not supply_hash:
            self._log("supply tx not broadcast", "ERR")
            self.results["lending_supply"] = "send-failed"
            return
        if not self.wait(supply_hash, "supply WETH"):
            self.results["lending_supply"] = "reverted"
            return
        self.results["lending_supply"] = supply_hash
        self._log("WETH supplied", "OK")

        # --- 3. wait until the pool credits the collateral ----------------- #
        avail = 0
        for _ in range(12):
            try:
                _, _, avail, _ = pool.functions.getUserAccountData(self.address).call()
            except Exception:
                avail = 0
            if avail > 0:
                break
            time.sleep(3)
        if avail == 0:
            self._log("pool reports zero borrow capacity after supply — skipping borrow", "WARN")
            self.results["lending_borrow"] = "no-capacity"
            return
        self._log(f"borrow capacity: {avail / 1e18:.4f} USD", "INFO")

        # --- 4. USDC faucet + borrow --------------------------------------- #
        if self.usdc_balance() < borrow_usdc:
            h = self._send(self.erc20(LENDING_USDC).functions.faucet(self.address, usdc_raw))
            if h:
                self.wait(h, "USDC faucet")
        borrow_hash = self._send(pool.functions.borrow(self.w3.to_checksum_address(LENDING_USDC), usdc_raw))
        if borrow_hash and self.wait(borrow_hash, "borrow USDC"):
            self.results["lending_borrow"] = borrow_hash
        else:
            self.results["lending_borrow"] = "failed"

    def weth_balance(self) -> float:
        return self.token_balance(LENDING_WETH)

    def usdc_balance(self) -> float:
        return self.token_balance(LENDING_USDC)

    # ================================================================== #
    #  PHASE 5 — NFT mints
    # ================================================================== #
    def step_nft(self) -> dict:
        out = {}
        for label, addr, qid in [
            ("genesis", NFT_GENESIS, "quest-nuke-genesis-mint"),
            ("verdant", NFT_VERDANT, "quest-nuke-verdant-mint"),
            ("cyan", NFT_CYAN, "quest-nuke-cyan-mint"),
        ]:
            c = self.w3.eth.contract(address=self.w3.to_checksum_address(addr), abi=NFT_ABI)
            try:
                name = c.functions.NAME().call()
            except Exception:
                name = label
            try:
                minted = c.functions.totalMinted().call()
                cap = c.functions.MAX_SUPPLY().call()
                price = c.functions.mintPrice().call()
                own = c.functions.balanceOf(self.address, 1).call()
            except Exception as e:
                self._log(f"NFT {label} read failed: {e}", "WARN")
                out[qid] = "read-failed"
                continue

            if own > 0:
                self._log(f"NFT {label}: already minted by this wallet", "SKIP")
                out[qid] = "already-owned"
                continue
            if minted >= cap:
                self._log(f"NFT {label}: SOLD OUT ({minted}/{cap}) — quest unreachable", "SKIP")
                out[qid] = "sold-out"
                continue
            if self.balance() < float(self.w3.from_wei(price, "ether")) + 0.00005:
                self._log(f"NFT {label}: insufficient ETH (price {self.w3.from_wei(price, 'ether')})", "WARN")
                out[qid] = "no-funds"
                continue

            self._log(f"minting NFT {label} @ {self.w3.from_wei(price, 'ether')} ETH", "STEP")
            h = self._send(c.functions.mint(1), value=price, gas_hint=250000)
            if h:
                rc = self.wait(h, f"mint {label}")
                out[qid] = h if rc else "failed"
        return out

    # ================================================================== #
    #  PHASE 6 — deploy + verify a contract
    # ================================================================== #
    def _forge(self) -> str:
        import shutil

        p = os.getenv("FORGE_BIN") or shutil.which("forge")
        if p:
            return p
        cand = os.path.expanduser("~/.foundry/bin/forge")
        if os.path.exists(cand):
            return cand
        raise FileNotFoundError("forge not found — install foundry or set FORGE_BIN")

    def step_deploy(self) -> tuple[str | None, str | None]:
        """Deploy contracts/KryvoraDemo.sol with forge. Returns (address, tx_hash)."""
        import subprocess

        if self.dry:
            self._log("DRY-RUN: would deploy KryvoraDemo.sol", "SKIP")
            return None, None
        self._log("deploying KryvoraDemo.sol via forge", "STEP")
        root = os.path.dirname(os.path.abspath(__file__))
        try:
            forge = self._forge()
        except FileNotFoundError as e:
            self._log(str(e), "ERR")
            return None, None
        cmd = [
            forge, "create", "contracts/KryvoraDemo.sol:KryvoraDemo",
            "--rpc-url", RPC,
            "--private-key", self.pk,
            "--broadcast",
            "--json",
        ]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, cwd=root, timeout=240)
        except Exception as e:
            self._log(f"forge create error: {e}", "ERR")
            return None, None
        out = (p.stdout or "") + (p.stderr or "")
        d = None
        raw = (p.stdout or "").strip()
        if raw:
            # forge --json prints a pretty-printed object on stdout
            try:
                d = json.loads(raw)
            except Exception:
                m = re.search(r"\{[\s\S]*\"deployedTo\"[\s\S]*\}", raw)
                if m:
                    try:
                        d = json.loads(m.group(0))
                    except Exception:
                        d = None
        if d and d.get("deployedTo"):
            addr, txh = d.get("deployedTo"), d.get("transactionHash")
            self._log(f"deployed at {addr} tx {txh}", "OK")
            return addr, txh
        self._log(f"forge create failed/unparsed:\n{out[-500:]}", "ERR")
        return None, None

    def step_verify_contract(self, address: str) -> bool:
        import subprocess

        if self.dry:
            return False
        self._log("verifying contract on Blockscout", "STEP")
        root = os.path.dirname(os.path.abspath(__file__))
        try:
            forge = self._forge()
        except FileNotFoundError as e:
            self._log(str(e), "ERR")
            return False
        cmd = [
            forge, "verify-contract", address, "contracts/KryvoraDemo.sol:KryvoraDemo",
            "--rpc-url", RPC,
            "--verifier", "blockscout",
            "--verifier-url", EXPLORER + "/api/",
            "--watch",
            "--chain-id", str(CHAIN_ID),
        ]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, cwd=root, timeout=300)
        except Exception as e:
            self._log(f"forge verify error: {e}", "ERR")
            return False
        out = (p.stdout or "") + (p.stderr or "")
        low = out.lower()
        ok = ("successfully verified" in low
              or "pass - verified" in low
              or "already verified" in low)
        self._log(("verification ok" if ok else "verification output: " + out[-300:]), "OK" if ok else "WARN")
        return ok

    # ================================================================== #
    #  PHASE 7 — bridge Sepolia ETH -> Kryvora (needs an L1 key with Sepolia ETH)
    # ================================================================== #
    def step_bridge(self, amount_eth: float, confirmations: int = 6) -> str | None:
        """Deposit Sepolia ETH into the Kryvora bridge.

        The bridge at 0x803bc3… is the *legacy* OP-Stack standard bridge: it exposes
        `depositETH(uint32,bytes)` (0xb1a1a882), NOT `bridgeETHTo`. The dApps frontend
        sends raw calldata = selector + minGasLimit + bytes-offset + bytes-length and
        expects the `ETHDepositInitiated` event (0x35d79ab8…) with to == sender.
        """
        if not self.l1_key:
            self._log("no SEPOLIA_PRIVATE_KEY set — bridge skipped", "SKIP")
            return None
        w3l = self.Web3(self.Web3.HTTPProvider(SEPOLIA_RPC, request_kwargs={"timeout": 30}))
        acct = Account.from_key(self.l1_key)
        bal = w3l.eth.get_balance(acct.address)
        if bal <= 0:
            self._log("Sepolia balance is zero — bridge skipped", "WARN")
            return None

        # raw calldata exactly like the dApps frontend builds it:
        # selector + uint32 minGasLimit + offset(0x40) + length(0)
        min_gas = hex(BRIDGE_MIN_GAS_LIMIT)[2:].rjust(64, "0")
        offset = hex(64)[2:].rjust(64, "0")
        empty_len = "0" * 64
        data = "0xb1a1a882" + min_gas + offset + empty_len

        try:
            base = int(w3l.eth.gas_price)
        except Exception:
            base = self.Web3.to_wei(1, "gwei")
        gp = max(base, self.Web3.to_wei(1, "gwei"))
        # OP ResourceMetering inflates the real gas need far beyond eth_estimateGas:
        # at a 650k limit the live call reverts while 900k passes. Probe + wide margin.
        try:
            est = w3l.eth.estimate_gas({
                "from": acct.address,
                "to": w3l.to_checksum_address(L1_STANDARD_BRIDGE),
                "value": w3l.to_wei(amount_eth, "ether"),
                "data": data,
            })
            gas_limit = max(int(est * 1.8), 1_000_000)
        except Exception:
            gas_limit = 1_200_000
        gas_cost = gp * gas_limit
        # keep a 3% cushion on top of gas, then bridge whatever is left
        cushion = max(int(gas_cost * 0.03), self.Web3.to_wei(0.000005, "ether"))
        affordable = bal - gas_cost - cushion
        if affordable <= 0:
            self._log(f"Sepolia {w3l.from_wei(bal, 'ether'):.6f} ETH cannot cover gas "
                      f"({w3l.from_wei(gas_cost, 'ether'):.6f}) — bridge skipped", "WARN")
            return None
        target = w3l.to_wei(amount_eth, "ether")
        value = min(target, affordable)
        if value < self.Web3.to_wei(0.00001, "ether"):
            self._log(f"Sepolia {w3l.from_wei(bal, 'ether'):.6f} ETH leaves only "
                      f"{w3l.from_wei(value, 'ether'):.6f} to bridge — skipped", "WARN")
            return None
        self._log(f"bridging {w3l.from_wei(value, 'ether')} Sepolia ETH -> Kryvora "
                  f"(depositETH, gas {w3l.from_wei(gp, 'gwei')} gwei x {gas_limit})", "STEP")

        try:
            tx = {
                "from": acct.address,
                "to": w3l.to_checksum_address(L1_STANDARD_BRIDGE),
                "value": value,
                "data": data,
                "nonce": w3l.eth.get_transaction_count(acct.address, "pending"),
                "chainId": SEPOLIA_CHAIN_ID,
                "gas": gas_limit,
                "gasPrice": gp,
            }
            signed = acct.sign_transaction(tx)
            raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
            h = w3l.eth.send_raw_transaction(raw).hex()
        except Exception as e:
            self._log(f"bridge tx failed: {str(e)[:220]}", "ERR")
            return None

        rc = w3l.eth.wait_for_transaction_receipt(h, timeout=240)
        if rc.get("status") != 1:
            self._log(f"bridge deposit REVERTED ({h}) — check the bridge selector", "ERR")
            return None
        topic = "0x35d79ab81f2b2017e19afb5c5571778877782d7a8786f5907f93b0f4702f4f23"
        has_event = any(
            lg["address"].lower() == L1_STANDARD_BRIDGE.lower()
            and (lg["topics"][0].hex() if hasattr(lg["topics"][0], "hex") else lg["topics"][0]).lower() == topic
            for lg in rc["logs"]
        )
        self._log(f"bridge deposit ok {h} (ETHDepositInitiated event: {has_event})", "OK")

        # the portal requires confirmations before it accepts the deposit
        start = rc["blockNumber"]
        self._log(f"waiting for {confirmations} Sepolia confirmations before verifying", "INFO")
        for _ in range(40):
            time.sleep(10)
            if w3l.eth.block_number >= start + confirmations:
                break
        return h

    # ================================================================== #
    #  Orchestration
    # ================================================================== #
    def run(self) -> dict:
        only = self.cfg.get("only") or set()
        def want(step):
            return (not only) or (step in only)

        self._log(f"=== Kryvora bot start · balance {self.balance():.6f} ETH ===", "TASK")

        if not self.step_auth():
            self.results["auth"] = "failed"
            return self.results
        self.results["auth"] = "ok"
        self.start_points = self.user.get("points", 0)

        # --- zero cost -------------------------------------------------- #
        if want("free"):
            self.step_free_quests()

        # --- faucet ------------------------------------------------------ #
        if want("faucet"):
            funded = False
            if self.balance() < 0.0005:
                funded = self.step_faucet()
            else:
                self._log(f"already funded ({self.balance():.6f} ETH) — skipping faucet claim", "SKIP")
            # the portal detects the payout itself; ask it to look either way
            if funded or self.balance() > 0:
                self.portal.verify_then_claim("quest-faucet", retries=(0, 5, 10, 20))

        # --- l2 tx ------------------------------------------------------- #
        if want("l2tx"):
            self.step_l2_tx()

        # --- DEX --------------------------------------------------------- #
        if want("swap"):
            tok = self.cfg.get("swap_token") or KALPHA
            amt = self.spendable(env_float("SWAP_ETH", 0.0003))
            h = self.step_swap(tok, amt) if amt > 0 else None
            if not h:
                self._log("swap skipped (no spendable ETH)", "SKIP")
            if h:
                self.portal.verify_then_claim("quest-daily-swap", tx_hash=h, retries=(0, 3))
                self.results["swap"] = h
        if want("lp"):
            tok = self.cfg.get("swap_token") or KALPHA
            amt = self.spendable(env_float("LP_ETH", 0.0003))
            h = self.step_lp(tok, amt) if amt > 0 else None
            if not h:
                self._log("LP skipped (no spendable ETH)", "SKIP")
            if h:
                self.portal.verify_then_claim("quest-provide-liquidity", tx_hash=h, retries=(0, 3))

        # --- lending ----------------------------------------------------- #
        if want("lending"):
            self.step_lending()
            if isinstance(self.results.get("lending_supply"), str) and self.results["lending_supply"].startswith("0x"):
                self.portal.verify_then_claim("quest-lending-supply", tx_hash=self.results["lending_supply"], retries=(0, 3))
            if isinstance(self.results.get("lending_borrow"), str) and self.results["lending_borrow"].startswith("0x"):
                self.portal.verify_then_claim("quest-lending-borrow", tx_hash=self.results["lending_borrow"], retries=(0, 3))

        # --- NFT --------------------------------------------------------- #
        if want("nft"):
            nft_res = self.step_nft()
            self.results.update(nft_res)
            for qid, v in nft_res.items():
                if isinstance(v, str) and v.startswith("0x"):
                    self.portal.verify_then_claim(qid, tx_hash=v, retries=(0, 3))

        # --- deploy + verify --------------------------------------------- #
        if want("deploy"):
            addr, txh = self.step_deploy()
            if txh:
                self.portal.verify_then_claim("quest-deploy", tx_hash=txh, retries=(0, 3))
            if addr:
                if self.step_verify_contract(addr):
                    self.portal.verify_then_claim("quest-verify-contract", tx_hash=txh or "", retries=(0, 3))

        # --- bridge ------------------------------------------------------ #
        if want("bridge"):
            before = self.balance()
            b = self.step_bridge(self.cfg.get("bridge_amount", 0.01))
            self.results["bridge_tx"] = b or "skipped"
            if b:
                # wait for the actual L2 credit (balance must GROW, not just be > 0)
                self._log(f"waiting for the L2 credit (balance now {before:.6f} ETH)", "INFO")
                for _ in range(30):
                    time.sleep(15)
                    if self.balance() > before + 0.000001:
                        self._log(f"L2 credit arrived: {self.balance():.6f} ETH", "OK")
                        break
                self.portal.verify_then_claim("quest-bridge", tx_hash=b, retries=(0, 30, 60, 120, 180))

        # --- blockscout explore (needs any confirmed tx hash) ------------ #
        if want("explore"):
            ref = self.results.get("l2_tx") or self.results.get("swap")
            if not ref and not self.dry:
                ref = self.step_l2_tx()
            self.portal.verify_then_claim("quest-explore-blockscout", tx_hash=ref or "", retries=(0, 3))

        # --- sweep everything claimable ---------------------------------- #
        self.portal.claim_all()
        u = self.portal.me()
        gained = u.get("points", 0) - getattr(self, "start_points", 0)
        self._log(f"=== done · {u.get('points')} pts (+{gained} this run) · rank #{u.get('rank')} ===", "TASK")
        self.results["points"] = u.get("points")
        self.results["gained"] = gained
        return self.results


# --------------------------------------------------------------------------- #
#  Multi-wallet runner
# --------------------------------------------------------------------------- #
def load_wallets() -> list[str]:
    keys: list[str] = []
    raw = os.getenv("WALLETS")
    if raw:
        try:
            for w in json.loads(raw):
                pk = w.get("pk") or w.get("private_key") or w.get("evm_pk")
                if pk:
                    keys.append(pk)
        except Exception as e:
            log(f"WALLETS json parse error: {e}", "ERR")
    single = os.getenv("PRIVATE_KEY")
    if single:
        keys.append(single.strip())
    multi = os.getenv("PRIVATE_KEYS")
    if multi:
        keys += [k.strip() for k in multi.split(",") if k.strip()]
    # dedupe preserving order
    seen, out = set(), []
    for k in keys:
        k = k if k.startswith("0x") else "0x" + k
        if k.lower() not in seen:
            seen.add(k.lower())
            out.append(k)
    return out


def generate_wallets(n: int) -> None:
    accts = [Account.create() for _ in range(n)]
    print(f"\n{'=' * 74}\n  {n} NEW WALLETS — private keys printed ONCE. Save them now.\n{'=' * 74}")
    entries = []
    for a in accts:
        print(f"  address {a.address}\n  pk      {a.key.hex()}\n")
        entries.append({"pk": a.key.hex(), "evm_address": a.address})
    print("WALLETS=" + json.dumps(entries))
    print("=" * 74)
    print("Fund each address from https://faucet-testnet.kryvora.network (0.001 ETH).")
    print("Put the WALLETS= line into .env to run them.\n")


def build_cfg(args) -> dict:
    only = set()
    if args.only:
        only = {s.strip() for s in args.only.split(",") if s.strip()}
    return {
        "proxy": args.proxy or os.getenv("PROXY") or None,
        "portal_proxy": os.getenv("PORTAL_PROXY") or None,
        "captcha_key": args.captcha or os.getenv("TWOCAPTCHA_KEY") or os.getenv("CAPTCHA_KEY") or "",
        "sepolia_pk": os.getenv("SEPOLIA_PRIVATE_KEY") or None,
        "bridge_amount": env_float("BRIDGE_AMOUNT_ETH", 0.01),
        "swap_token": os.getenv("SWAP_TOKEN") or KALPHA,
        "referral": os.getenv("REFERRAL_CODE") or None,
        "dry": args.dry,
        "only": only,
    }


def main():
    ap = argparse.ArgumentParser(description="Kryvora Network campaign bot")
    ap.add_argument("--gen", type=int, help="generate N new wallets and exit")
    ap.add_argument("--fund", action="store_true", help="claim the faucet for each wallet (uses the proxy) and exit")
    ap.add_argument("--status", action="store_true", help="print portal status for each wallet and exit")
    ap.add_argument("--dry", action="store_true", help="auth + free quests only, no on-chain writes")
    ap.add_argument("--only", help="comma list: free,faucet,l2tx,swap,lp,lending,nft,deploy,bridge,explore")
    ap.add_argument("--proxy", help="http(s) proxy for all requests")
    ap.add_argument("--captcha", help="2captcha API key (overrides .env)")
    args = ap.parse_args()

    if args.gen:
        generate_wallets(args.gen)
        return

    keys = load_wallets()
    if not keys:
        log("no wallets configured — run: python3 kryvora_bot.py --gen 1  then put WALLETS= into .env", "ERR")
        sys.exit(1)

    cfg = build_cfg(args)
    log(f"{len(keys)} wallet(s) loaded" + (" · DRY-RUN" if cfg["dry"] else ""), "INFO")

    if args.status:
        for pk in keys:
            try:
                acct = Account.from_key(pk)
                p = Portal(cfg["proxy"])
                u = p.login(acct)
                log_acct(acct.address, f"{u.get('points')} pts · rank #{u.get('rank')} · "
                                       f"{u.get('completedQuestsCount')} quests · "
                                       f"claimable={len(u.get('claimableQuestIds', []))}", "OK")
            except Exception as e:
                log(f"status failed: {e}", "ERR")
        return

    if args.fund:
        for i, pk in enumerate(keys, 1):
            acct = Account.from_key(pk)
            log(f"----- funding wallet {i}/{len(keys)}: {acct.address} -----", "TASK")
            try:
                bot = KryvoraBot(pk, cfg)
                before = bot.balance()
                if before > 0.0004:
                    log_acct(acct.address, f"already funded ({before:.6f} ETH)", "SKIP")
                    continue
                # rotate to a fresh proxy IP for every wallet (per-IP faucet limit)
                if cfg["proxy"]:
                    bot.proxy = rotate_proxy(cfg["proxy"], i)
                    bot.solver.proxy = bot.proxy
                    log_acct(acct.address, f"proxy session {i}: {redact(bot.proxy)}", "INFO")
                bot.step_faucet()
            except Exception:
                log("fund failed:\n" + traceback.format_exc()[-600:], "ERR")
            if i < len(keys):
                time.sleep(10)
        return

    # --- run --- #
    while True:
        for i, pk in enumerate(keys, 1):
            log(f"----- wallet {i}/{len(keys)} -----", "TASK")
            try:
                bot = KryvoraBot(pk, cfg)
                bot.run()
            except Exception:
                log("wallet crashed:\n" + traceback.format_exc()[-900:], "ERR")
            if i < len(keys):
                time.sleep(env_int("DELAY_BETWEEN_WALLETS", 15))

        if not env_bool("AUTO_LOOP", False):
            break
        sleep_s = env_int("DELAY_AFTER_CYCLE", 86400)
        log(f"cycle complete — sleeping {sleep_s}s ({sleep_s / 3600:.1f}h) before next run", "INFO")
        time.sleep(sleep_s)


if __name__ == "__main__":
    main()
