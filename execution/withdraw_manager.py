"""
Withdraw manager — VERIFIED untuk web3.py 7.x (raw_transaction snake_case).

Pipeline:
1. Hyperliquid → Arbitrum (USDC) via native bridge
2. Arbitrum: USDC → USDT via Uniswap V3
3. Send USDT ke MetaMask user

⚠️ TEST DULU dengan amount kecil ($5-10) sebelum live full!
"""
import time
import json
from datetime import datetime, timezone
from loguru import logger
from web3 import Web3
from eth_account import Account

from config import (
    HYPERLIQUID_PRIVATE_KEY, HYPERLIQUID_ACCOUNT,
    ARBITRUM_PRIVATE_KEY, ARBITRUM_RPC_URL,
    DESTINATION_USDT_WALLET,
    USDC_TOKEN_ARB, USDT_TOKEN_ARB,
    UNISWAP_V3_ROUTER, UNISWAP_USDC_USDT_FEE,
    WITHDRAW_PROFIT_PCT, WITHDRAW_THRESHOLD_USD, WITHDRAW_MIN_INTERVAL_HOURS,
    DRY_RUN, get_api_url, DATA_DIR,
)
from hyperliquid.exchange import Exchange


# ERC20 minimal ABI
ERC20_ABI = json.loads("""
[
    {"constant":true,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"},
    {"constant":false,"inputs":[{"name":"_to","type":"address"},{"name":"_value","type":"uint256"}],"name":"transfer","outputs":[{"name":"","type":"bool"}],"type":"function"},
    {"constant":false,"inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"},
    {"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"}
]
""")

UNISWAP_V3_ABI = json.loads("""
[
    {
        "inputs": [
            {
                "components": [
                    {"name":"tokenIn","type":"address"},
                    {"name":"tokenOut","type":"address"},
                    {"name":"fee","type":"uint24"},
                    {"name":"recipient","type":"address"},
                    {"name":"deadline","type":"uint256"},
                    {"name":"amountIn","type":"uint256"},
                    {"name":"amountOutMinimum","type":"uint256"},
                    {"name":"sqrtPriceLimitX96","type":"uint160"}
                ],
                "name":"params",
                "type":"tuple"
            }
        ],
        "name":"exactInputSingle",
        "outputs":[{"name":"amountOut","type":"uint256"}],
        "stateMutability":"payable",
        "type":"function"
    }
]
""")


WITHDRAW_STATE_FILE = DATA_DIR / "withdraw_state.json"


class WithdrawManager:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        if HYPERLIQUID_PRIVATE_KEY:
            hl_wallet = Account.from_key(HYPERLIQUID_PRIVATE_KEY)
            self.hl_exchange = Exchange(
                hl_wallet, base_url=get_api_url(),
                account_address=HYPERLIQUID_ACCOUNT,
            )
        
        self.w3 = Web3(Web3.HTTPProvider(ARBITRUM_RPC_URL))
        if ARBITRUM_PRIVATE_KEY:
            self.arb_account = self.w3.eth.account.from_key(ARBITRUM_PRIVATE_KEY)
        else:
            self.arb_account = None
        
        self.state = self._load_state()
        self._initialized = True
    
    def _load_state(self) -> dict:
        if WITHDRAW_STATE_FILE.exists():
            with open(WITHDRAW_STATE_FILE) as f:
                return json.load(f)
        return {"cumulative_profit_pending": 0.0, "last_withdraw_at": None}
    
    def _save_state(self):
        WITHDRAW_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(WITHDRAW_STATE_FILE, "w") as f:
            json.dump(self.state, f, indent=2)
    
    def record_profit(self, profit_usd: float):
        if profit_usd <= 0:
            return
        amount_to_pend = profit_usd * WITHDRAW_PROFIT_PCT
        self.state["cumulative_profit_pending"] += amount_to_pend
        self._save_state()
        logger.info(f"Pending withdraw: ${self.state['cumulative_profit_pending']:.2f}")
    
    def should_withdraw(self) -> bool:
        if self.state["cumulative_profit_pending"] < WITHDRAW_THRESHOLD_USD:
            return False
        if self.state["last_withdraw_at"]:
            last_dt = datetime.fromisoformat(self.state["last_withdraw_at"])
            hours_since = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
            if hours_since < WITHDRAW_MIN_INTERVAL_HOURS:
                return False
        return True
    
    def execute_withdraw(self) -> bool:
        from notifications.telegram import notify_withdrawal, notify_critical_error
        from monitoring.trade_logger import (
            log_withdrawal_initiated, log_withdrawal_complete, log_withdrawal_failed
        )
        from monitoring.tax_logger import log_taxable_event
        
        amount = self.state["cumulative_profit_pending"]
        if amount < WITHDRAW_THRESHOLD_USD:
            return False
        
        if DRY_RUN:
            logger.info(f"[DRY_RUN] Would withdraw ${amount:.2f}")
            self.state["cumulative_profit_pending"] = 0
            self.state["last_withdraw_at"] = datetime.now(timezone.utc).isoformat()
            self._save_state()
            return True
        
        withdrawal_id = log_withdrawal_initiated(amount)

        try:
            hl_tx = self._hl_withdraw_to_arbitrum(amount)
            # Bug #10: use actual arrived balance (after bridge fee) for the swap amount
            balance_wei = self._wait_for_usdc_arrival(amount)
            actual_usdc = balance_wei / 10**6  # USDC has 6 decimals
            swap_tx, usdt_amount = self._swap_usdc_to_usdt(actual_usdc * 0.999)
            send_tx = self._send_usdt_to_destination(usdt_amount)
            
            log_withdrawal_complete(withdrawal_id, swap_tx, send_tx)
            log_taxable_event(
                "withdrawal_to_external", "USDT", amount,
                notes=f"HL→ARB→MetaMask. HL tx: {hl_tx[:16]}",
            )
            notify_withdrawal(amount, send_tx, "complete")
            
            self.state["cumulative_profit_pending"] = 0
            self.state["last_withdraw_at"] = datetime.now(timezone.utc).isoformat()
            self._save_state()
            
            return True
            
        except Exception as e:
            logger.exception(f"Withdraw failed: {e}")
            log_withdrawal_failed(withdrawal_id, str(e))
            notify_critical_error(f"Withdraw failed: {e}", "withdraw_failed")
            return False
    
    def _hl_withdraw_to_arbitrum(self, amount_usd: float) -> str:
        if not self.arb_account:
            raise RuntimeError("ARBITRUM_PRIVATE_KEY not configured")
        
        result = self.hl_exchange.withdraw_from_bridge(
            amount_usd, self.arb_account.address
        )
        if result["status"] != "ok":
            raise RuntimeError(f"HL withdraw failed: {result}")
        
        logger.info(f"HL withdraw initiated: ${amount_usd:.2f}")
        return str(result.get("response", "unknown"))
    
    def _wait_for_usdc_arrival(self, expected_amount_usd: float, timeout_seconds: int = 600) -> int:
        from config import HL_BRIDGE_FEE_USD

        usdc = self.w3.eth.contract(
            address=Web3.to_checksum_address(USDC_TOKEN_ARB),
            abi=ERC20_ABI,
        )
        decimals = usdc.functions.decimals().call()
        # Bug #10: account for flat bridge fee before applying the 5% safety buffer
        expected_after_fee = max(0.5, expected_amount_usd - HL_BRIDGE_FEE_USD)
        expected_wei = int(expected_after_fee * 0.95 * 10**decimals)

        start = time.time()
        while time.time() - start < timeout_seconds:
            balance = usdc.functions.balanceOf(self.arb_account.address).call()
            if balance >= expected_wei:
                logger.info(f"USDC arrived: {balance / 10**decimals:.4f}")
                return balance
            time.sleep(15)

        raise TimeoutError(f"USDC not arrived in {timeout_seconds}s")
    
    def _swap_usdc_to_usdt(self, amount_usd: float) -> tuple[str, float]:
        """Swap USDC → USDT via Uniswap V3.

        ⚠️ web3.py 7.x: signed_tx.raw_transaction (snake_case), BUKAN rawTransaction.
        """
        usdc = self.w3.eth.contract(
            address=Web3.to_checksum_address(USDC_TOKEN_ARB), abi=ERC20_ABI
        )
        usdt = self.w3.eth.contract(
            address=Web3.to_checksum_address(USDT_TOKEN_ARB), abi=ERC20_ABI
        )
        router = self.w3.eth.contract(
            address=Web3.to_checksum_address(UNISWAP_V3_ROUTER), abi=UNISWAP_V3_ABI
        )

        usdc_decimals = 6
        amount_in = int(amount_usd * 10**usdc_decimals)

        # Bug #9: snapshot USDT balance before swap to compute actual received
        usdt_balance_before = usdt.functions.balanceOf(self.arb_account.address).call()

        # 1. Approve router
        approve_tx = usdc.functions.approve(
            UNISWAP_V3_ROUTER, amount_in
        ).build_transaction({
            "from": self.arb_account.address,
            "nonce": self.w3.eth.get_transaction_count(self.arb_account.address),
            "gas": 100000,
            "gasPrice": self.w3.eth.gas_price,
        })
        signed_approve = self.arb_account.sign_transaction(approve_tx)
        # web3.py 7.x: raw_transaction (snake_case)
        approve_hash = self.w3.eth.send_raw_transaction(signed_approve.raw_transaction)
        self.w3.eth.wait_for_transaction_receipt(approve_hash, timeout=120)

        # 2. Swap
        deadline = int(time.time() + 300)
        amount_out_min = int(amount_in * 0.995)

        params = (
            Web3.to_checksum_address(USDC_TOKEN_ARB),
            Web3.to_checksum_address(USDT_TOKEN_ARB),
            UNISWAP_USDC_USDT_FEE,
            self.arb_account.address,
            deadline,
            amount_in,
            amount_out_min,
            0,
        )

        swap_tx = router.functions.exactInputSingle(params).build_transaction({
            "from": self.arb_account.address,
            "nonce": self.w3.eth.get_transaction_count(self.arb_account.address),
            "gas": 250000,
            "gasPrice": self.w3.eth.gas_price,
            "value": 0,
        })
        signed = self.arb_account.sign_transaction(swap_tx)
        # web3.py 7.x: raw_transaction (snake_case)
        swap_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(swap_hash, timeout=120)

        if receipt.status != 1:
            raise RuntimeError(f"Swap failed: {swap_hash.hex()}")

        # Bug #9: measure actual USDT received from balance diff (not ~1:1 estimate)
        usdt_balance_after = usdt.functions.balanceOf(self.arb_account.address).call()
        usdt_decimals = 6
        usdt_received = (usdt_balance_after - usdt_balance_before) / 10**usdt_decimals

        logger.info(f"Swap: ${amount_usd:.2f} USDC → ${usdt_received:.4f} USDT (actual)")
        return swap_hash.hex(), usdt_received
    
    def _send_usdt_to_destination(self, amount_usdt: float) -> str:
        """Transfer USDT ke MetaMask user."""
        usdt = self.w3.eth.contract(
            address=Web3.to_checksum_address(USDT_TOKEN_ARB), abi=ERC20_ABI
        )
        
        decimals = 6
        balance = usdt.functions.balanceOf(self.arb_account.address).call()
        amount_to_send = min(int(amount_usdt * 10**decimals), balance)
        
        tx = usdt.functions.transfer(
            Web3.to_checksum_address(DESTINATION_USDT_WALLET),
            amount_to_send,
        ).build_transaction({
            "from": self.arb_account.address,
            "nonce": self.w3.eth.get_transaction_count(self.arb_account.address),
            "gas": 100000,
            "gasPrice": self.w3.eth.gas_price,
        })
        signed = self.arb_account.sign_transaction(tx)
        # web3.py 7.x: raw_transaction (snake_case)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        
        logger.info(f"USDT sent: ${amount_usdt:.2f} → {DESTINATION_USDT_WALLET}")
        return tx_hash.hex()