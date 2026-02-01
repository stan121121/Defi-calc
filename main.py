"""
=============================================================================
DeFi Risk.calc - Финальная версия v2.1
=============================================================================

Изменения v2.1:
✅ Новый порядок ввода: Max LTV → LT → режим расчета
✅ Цена ликвидации учитывает источник цены (ручной/авто)
✅ В расчете показывается, какая цена была использована

dev. by Taponni

=============================================================================
"""

import asyncio
import os
from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.strategy import FSMStrategy
from typing import Tuple, Optional, Dict
import aiohttp
from datetime import datetime, timedelta
from collections import deque

# =============================================================================
# PRICE FETCHER
# =============================================================================

class CoinGeckoPriceFetcher:
    """Price fetcher с кэшированием и rate limiting"""
    
    COINGECKO_IDS = {
        "ETH": "ethereum",
        "BTC": "bitcoin",
        "SOL": "solana",
        "USDC": "usd-coin",
        "USDT": "tether",
        "DAI": "dai",
        "BUSD": "binance-usd",
        "BNB": "binancecoin",
        "ADA": "cardano",
        "DOT": "polkadot",
        "AVAX": "avalanche-2",
        "MATIC": "matic-network",
        "LINK": "chainlink",
        "UNI": "uniswap",
        "ATOM": "cosmos",
        "XRP": "ripple",
        "LTC": "litecoin",
        "DOGE": "dogecoin",
        "SHIB": "shiba-inu",
        "AAVE": "aave",
    }
    
    BASE_URL = "https://api.coingecko.com/api/v3"
    
    def __init__(self, cache_ttl: int = 300, max_requests_per_minute: int = 5):
        self._cache: Dict[str, Tuple[float, datetime]] = {}
        self._cache_ttl = timedelta(seconds=cache_ttl)
        self._session: Optional[aiohttp.ClientSession] = None
        self._max_requests_per_minute = max_requests_per_minute
        self._request_times = deque(maxlen=max_requests_per_minute)
        self._rate_limit_lock = asyncio.Lock()
        self._stats = {"total_requests": 0, "cache_hits": 0, "api_calls": 0}
    
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=15)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session
    
    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
    
    async def _wait_for_rate_limit(self):
        async with self._rate_limit_lock:
            now = datetime.now()
            while self._request_times and (now - self._request_times[0]).total_seconds() > 60:
                self._request_times.popleft()
            if len(self._request_times) >= self._max_requests_per_minute:
                oldest_request = self._request_times[0]
                wait_time = 60 - (now - oldest_request).total_seconds()
                if wait_time > 0:
                    await asyncio.sleep(wait_time + 0.5)
            self._request_times.append(now)
    
    def _get_from_cache(self, symbol: str) -> Optional[float]:
        if symbol in self._cache:
            price, timestamp = self._cache[symbol]
            if datetime.now() - timestamp < self._cache_ttl:
                self._stats["cache_hits"] += 1
                return price
        return None
    
    def _save_to_cache(self, symbol: str, price: float):
        self._cache[symbol] = (price, datetime.now())
    
    def get_stats(self) -> dict:
        cache_hit_rate = (
            self._stats["cache_hits"] / self._stats["total_requests"] * 100 
            if self._stats["total_requests"] > 0 else 0
        )
        return {**self._stats, "cache_hit_rate": f"{cache_hit_rate:.1f}%", "cache_size": len(self._cache)}
    
    async def get_price_usd(self, symbol: str, use_cache: bool = True) -> Optional[float]:
        symbol = symbol.upper().strip()
        self._stats["total_requests"] += 1
        
        if use_cache:
            cached_price = self._get_from_cache(symbol)
            if cached_price is not None:
                return cached_price
        
        if symbol not in self.COINGECKO_IDS:
            return None
        
        url = f"{self.BASE_URL}/simple/price"
        params = {"ids": self.COINGECKO_IDS[symbol], "vs_currencies": "usd"}
        
        try:
            await self._wait_for_rate_limit()
            session = await self._get_session()
            self._stats["api_calls"] += 1
            
            async with session.get(url, params=params) as response:
                if response.status == 429:
                    retry_after = int(response.headers.get('Retry-After', '60'))
                    await asyncio.sleep(retry_after)
                    return await self.get_price_usd(symbol, use_cache=False)
                
                response.raise_for_status()
                data = await response.json()
                
                coin_id = self.COINGECKO_IDS[symbol]
                if coin_id not in data or "usd" not in data[coin_id]:
                    return None
                
                price = data[coin_id]["usd"]
                if use_cache:
                    self._save_to_cache(symbol, price)
                return price
        except Exception as e:
            print(f"❌ Ошибка получения цены {symbol}: {e}")
            return None
    
    @classmethod
    def is_supported(cls, symbol: str) -> bool:
        return symbol.upper().strip() in cls.COINGECKO_IDS
    
    @classmethod
    def get_supported_symbols(cls) -> list:
        return sorted(cls.COINGECKO_IDS.keys())


# =============================================================================
# CONFIGURATION
# =============================================================================

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("❌ Не установлен токен бота! Создайте .env файл с BOT_TOKEN=ваш_токен")

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=MemoryStorage(), fsm_strategy=FSMStrategy.USER_IN_CHAT)

price_fetcher = CoinGeckoPriceFetcher(cache_ttl=300, max_requests_per_minute=5)


# =============================================================================
# FSM STATES - НОВЫЙ ПОРЯДОК
# =============================================================================

class Calc(StatesGroup):
    """Состояния для расчета позиции"""
    supply_ticker = State()         # Тикер залога
    borrow_ticker = State()         # Тикер займа
    supply_amount = State()         # Количество залога
    supply_price_manual = State()  # Ручной ввод цены залога
    max_ltv = State()               # Maximum LTV (ПЕРВЫЙ параметр!)
    lt = State()                    # Liquidation Threshold (ВТОРОЙ параметр!)
    mode = State()                  # Режим расчета (ТРЕТИЙ!)
    ltv = State()                   # LTV (если режим по LTV)
    borrow = State()                # Сумма займа (если режим по сумме)


# =============================================================================
# KEYBOARDS
# =============================================================================

mode_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🔢 По LTV", callback_data="mode_ltv")],
    [InlineKeyboardButton(text="💵 По сумме займа", callback_data="mode_borrow")]
])


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def validate_number(text: str, min_val: float = 0, max_val: Optional[float] = None) -> Tuple[bool, float, str]:
    try:
        text = text.replace(",", ".").strip()
        value = float(text)
        if value <= min_val:
            return False, 0, f"Значение должно быть больше {min_val}"
        if max_val is not None and value > max_val:
            return False, 0, f"Значение должно быть не больше {max_val}"
        return True, value, ""
    except (ValueError, TypeError):
        return False, 0, "Пожалуйста, введите корректное число"


def validate_ticker(text: str, max_length: int = 10) -> Tuple[bool, str, str]:
    ticker = text.upper().strip()
    if len(ticker) > max_length:
        return False, "", f"Тикер слишком длинный (максимум {max_length} символов)"
    if not ticker.isalnum():
        return False, "", "Тикер должен содержать только буквы и цифры"
    return True, ticker, ""


def format_currency(value: float) -> str:
    if value >= 1_000_000:
        return f"${value/1_000_000:.2f}M"
    elif value >= 1_000:
        return f"${value/1_000:.1f}K"
    else:
        return f"${value:.2f}"


def format_number(value: float, decimals: int = 2) -> str:
    if value == float('inf'):
        return "∞"
    return f"{value:.{decimals}f}"


def calculate_health_factor(collateral: float, lt: float, borrow: float) -> float:
    if borrow <= 0:
        return float('inf')
    return (collateral * lt) / borrow


def calculate_liquidation_price(borrow: float, supply_amount: float, lt: float) -> float:
    """
    Рассчитывает цену ликвидации
    При этой цене залога позиция будет ликвидирована
    """
    denominator = supply_amount * lt
    if denominator <= 0:
        return 0
    return borrow / denominator


def get_position_status(hf: float) -> Tuple[str, str]:
    if hf <= 1.0:
        return "🔴 ЛИКВИДАЦИЯ", "🔴"
    elif hf < 1.3:
        return "🟡 ВНИМАНИЕ", "🟡"
    elif hf < 2.0:
        return "🟢 БЕЗОПАСНО", "🟢"
    else:
        return "🔵 ОЧЕНЬ БЕЗОПАСНО", "🔵"


def build_result_message(data: dict, calculations: dict) -> str:
    """Формирует итоговое сообщение с результатами"""
    status, emoji = get_position_status(calculations['hf'])
    price_source = data.get('supply_price_source', 'manual')
    
    # Умное форматирование цены (больше знаков для маленьких цен)
    price = calculations['price']
    if price >= 1:
        price_str = f"${price:,.2f}"
    elif price >= 0.01:
        price_str = f"${price:.4f}"
    else:
        price_str = f"${price:.8f}"
    
    # Аналогично для цены ликвидации
    liq_price = calculations['liq_price']
    if liq_price >= 1:
        liq_price_str = f"${liq_price:,.2f}"
    elif liq_price >= 0.01:
        liq_price_str = f"${liq_price:.4f}"
    else:
        liq_price_str = f"${liq_price:.8f}"
    
    # Определяем, как показывать цену
    if price_source == "auto":
        price_display = f"{price_str} (CoinGecko)"
    else:
        price_display = f"{price_str} (ручной ввод)"
    
    result = (
        f"<b>{emoji} РАСЧЕТ ПОЗИЦИИ</b>\n"
        f"Статус: <b>{status}</b>\n\n"
        
        f"<b>💎 ЗАЛОГ:</b>\n"
        f"• Актив: <b>{data['supply_ticker']}</b>\n"
        f"• Количество: {calculations['supply_amt']:.6f}\n"
        f"• Цена: {price_display}\n"
        f"• Стоимость: <b>{format_currency(calculations['collateral'])}</b>\n\n"
        
        f"<b>💰 ЗАЙМ:</b>\n"
        f"• Актив: <b>{data['borrow_ticker']}</b>\n"
        f"• Сумма: <b>{format_currency(calculations['borrow'])}</b>\n\n"
        
        f"<b>⚙️ ПАРАМЕТРЫ:</b>\n"
        f"• Maximum LTV: {calculations['max_ltv_percent']}%\n"
        f"• Liquidation Threshold: {calculations['lt']*100:.1f}%\n"
        f"• Current LTV: <b>{calculations['ltv_percent']:.2f}%</b>\n\n"
        
        f"<b>📊 РИСКИ:</b>\n"
        f"• Health Factor: <b>{format_number(calculations['hf'], 2)}</b>\n"
    )
    
    # Цена ликвидации с указанием источника цены
    if price_source == "manual":
        result += (
            f"• Цена ликвидации: <b>{liq_price_str}</b>\n"
            f"  <i>(при ручной цене залога {price_str})</i>\n"
        )
    else:
        result += f"• Цена ликвидации: <b>{liq_price_str}</b>\n"
    
    result += (
        f"• Буфер безопасности: <b>{calculations['buffer']:.1f}%</b>\n"
        f"• Макс. возможный займ: {format_currency(calculations['max_borrow'])}\n\n"
        
        f"<b>📉 СЦЕНАРИИ (падение цены):</b>\n"
    )
    
    for drop, scen_hf in calculations['scenarios']:
        new_price = calculations['price'] * (1 - drop / 100)
        # Умное форматирование для цен сценариев
        if new_price >= 1:
            new_price_str = f"${new_price:,.2f}"
        elif new_price >= 0.01:
            new_price_str = f"${new_price:.4f}"
        else:
            new_price_str = f"${new_price:.8f}"
        result += f"• -{drop}% ({new_price_str}) → HF: {format_number(scen_hf, 2)}\n"
    
    # Рекомендации
    if calculations['hf'] < 1.3:
        result += (
            "\n<b>⚠️ РЕКОМЕНДАЦИИ:</b>\n"
            "• Увеличьте залог для повышения HF\n"
            "• Уменьшите сумму займа\n"
            "• Подготовьте средства для пополнения\n"
            "• Установите алерты на изменение цены"
        )
    
    # Уведомление о ручном вводе
    if price_source == "manual":
        result += (
            f"\n\n💡 <i>Цена {data['supply_ticker']} введена вручную. "
            f"При следующем расчете потребуется ввести заново.</i>"
        )
    
    return result


# =============================================================================
# COMMAND HANDLERS
# =============================================================================

@dp.message(Command("start"))
async def start_cmd(msg: types.Message, state: FSMContext):
    """Начало работы"""
    await state.clear()
    supported = price_fetcher.get_supported_symbols()
    supported_preview = ", ".join(supported[:10])
    
    await msg.answer(
        "🤖 <b>DeFi Risk.calc</b>\n"
        "<i>Калькулятор кредитных позиций в DeFi</i>\n\n"
        
        f"<b>💰 Автоматические цены ({len(supported)} монет):</b>\n"
        f"{supported_preview}...\n\n"
        
        "💡 <b>Для любых других токенов:</b>\n"
        "Можно ввести цену вручную\n\n"
        
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Введите <b>тикер залогового актива</b>\n"
        "(например: ETH, BTC, SOL)"
    )
    await state.set_state(Calc.supply_ticker)


@dp.message(Command("reset", "cancel"))
async def reset_cmd(msg: types.Message, state: FSMContext):
    """Сброс расчета"""
    await state.clear()
    await msg.answer("✅ Расчет сброшен. Используйте /start для нового расчета")


@dp.message(Command("help"))
async def help_cmd(msg: types.Message):
    """Справка"""
    await msg.answer(
        "<b>📖 Справка</b>\n\n"
        "<b>Команды:</b>\n"
        "/start - начать расчет\n"
        "/reset - сбросить расчет\n"
        "/supported - список монет\n"
        "/stats - статистика API\n\n"
        
        "<b>Порядок ввода:</b>\n"
        "1️⃣ Тикер залога\n"
        "2️⃣ Тикер займа\n"
        "3️⃣ Количество залога\n"
        "4️⃣ Цена (авто/ручная)\n"
        "5️⃣ Maximum LTV\n"
        "6️⃣ Liquidation Threshold\n"
        "7️⃣ Режим расчета\n"
        "8️⃣ LTV или сумма займа"
    )


@dp.message(Command("supported"))
async def supported_cmd(msg: types.Message):
    """Список поддерживаемых монет"""
    supported = price_fetcher.get_supported_symbols()
    cols = 4
    rows = []
    for i in range(0, len(supported), cols):
        row = " | ".join(f"<code>{coin}</code>" for coin in supported[i:i+cols])
        rows.append(row)
    
    await msg.answer(
        f"<b>💎 Монеты с автоценами ({len(supported)})</b>\n\n"
        + "\n".join(rows) + 
        "\n\n💡 <i>Для остальных - ручной ввод</i>"
    )


@dp.message(Command("stats"))
async def stats_cmd(msg: types.Message):
    """Статистика API"""
    stats = price_fetcher.get_stats()
    await msg.answer(
        f"<b>📊 Статистика API</b>\n\n"
        f"Запросов: {stats['total_requests']}\n"
        f"API вызовов: {stats['api_calls']}\n"
        f"Из кэша: {stats['cache_hits']}\n"
        f"Процент кэша: {stats['cache_hit_rate']}"
    )


# =============================================================================
# STATE HANDLERS - НОВЫЙ ПОРЯДОК ВВОДА
# =============================================================================

@dp.message(Calc.supply_ticker)
async def process_supply_ticker(msg: types.Message, state: FSMContext):
    """Тикер залога"""
    valid, ticker, error = validate_ticker(msg.text)
    if not valid:
        await msg.answer(f"❌ {error}\n\nВведите корректный тикер:")
        return
    
    await state.update_data(supply_ticker=ticker)
    is_supported = price_fetcher.is_supported(ticker)
    
    await msg.answer(
        f"✅ <b>Залоговый актив:</b> {ticker}\n"
        f"{'🌐' if is_supported else '✍️'} Цена: {'автоматическая' if is_supported else 'ручной ввод'}\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Введите <b>тикер заимствуемого актива</b>"
    )
    await state.set_state(Calc.borrow_ticker)


@dp.message(Calc.borrow_ticker)
async def process_borrow_ticker(msg: types.Message, state: FSMContext):
    """Тикер займа"""
    valid, ticker, error = validate_ticker(msg.text)
    if not valid:
        await msg.answer(f"❌ {error}\n\nВведите корректный тикер:")
        return
    
    await state.update_data(borrow_ticker=ticker)
    data = await state.get_data()
    
    await msg.answer(
        f"✅ <b>Заимствуемый актив:</b> {ticker}\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"Введите <b>количество {data['supply_ticker']}</b>"
    )
    await state.set_state(Calc.supply_amount)


@dp.message(Calc.supply_amount)
async def process_supply_amount(msg: types.Message, state: FSMContext):
    """Количество залога"""
    valid, value, error = validate_number(msg.text, min_val=0.000001)
    if not valid:
        await msg.answer(f"❌ {error}\n\nВведите количество:")
        return
    
    await state.update_data(supply_amount=value)
    data = await state.get_data()
    ticker = data['supply_ticker']
    
    # Получение цены
    if price_fetcher.is_supported(ticker):
        await msg.answer(f"✅ Количество: {value:.6f}\n\n⏳ Получаю цену {ticker}...")
        
        price = await price_fetcher.get_price_usd(ticker)
        
        if price is None:
            await msg.answer(
                f"❌ Не удалось получить цену автоматически\n\n"
                f"Введите <b>цену {ticker}</b> в USD вручную:"
            )
            await state.set_state(Calc.supply_price_manual)
            return
        
        await state.update_data(supply_price=price, supply_price_source="auto")
        collateral_value = value * price
        
        # Умное форматирование цены
        if price >= 1:
            price_str = f"${price:,.2f}"
        elif price >= 0.01:
            price_str = f"${price:.4f}"
        elif price >= 0.0001:
            price_str = f"${price:.6f}"
        else:
            price_str = f"${price:.8f}"
        
        await msg.answer(
            f"✅ Цена (CoinGecko): <b>{price_str}</b>\n"
            f"💰 Стоимость залога: <b>{format_currency(collateral_value)}</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Введите <b>Maximum LTV</b> в %\n"
            "(например: 65)"
        )
        await state.set_state(Calc.max_ltv)
    else:
        await msg.answer(
            f"✅ Количество: {value:.6f}\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"Введите <b>цену {ticker}</b> в USD"
        )
        await state.set_state(Calc.supply_price_manual)


@dp.message(Calc.supply_price_manual)
async def process_supply_price_manual(msg: types.Message, state: FSMContext):
    """Ручной ввод цены"""
    valid, price, error = validate_number(msg.text, min_val=0.000001)
    if not valid:
        await msg.answer(f"❌ {error}\n\nВведите цену:")
        return
    
    data = await state.get_data()
    ticker = data['supply_ticker']
    amount = data['supply_amount']
    
    await state.update_data(supply_price=price, supply_price_source="manual")
    collateral_value = amount * price
    
    # Умное форматирование цены
    if price >= 1:
        price_str = f"${price:,.2f}"
    elif price >= 0.01:
        price_str = f"${price:.4f}"
    elif price >= 0.0001:
        price_str = f"${price:.6f}"
    else:
        price_str = f"${price:.8f}"
    
    await msg.answer(
        f"✅ Цена (ручной ввод): <b>{price_str}</b>\n"
        f"💰 Стоимость залога: <b>{format_currency(collateral_value)}</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Введите <b>Maximum LTV</b> в %\n"
        "(например: 65)"
    )
    await state.set_state(Calc.max_ltv)


@dp.message(Calc.max_ltv)
async def process_max_ltv(msg: types.Message, state: FSMContext):
    """Maximum LTV - ПЕРВЫЙ параметр"""
    valid, value, error = validate_number(msg.text, min_val=0, max_val=100)
    if not valid:
        await msg.answer(f"❌ {error}\n\nMax LTV должен быть 0-100%. Введите:")
        return
    
    await state.update_data(max_ltv=value / 100)
    
    # Получаем данные для расчёта максимального займа
    data = await state.get_data()
    supply_amount = data.get('supply_amount', 0)
    supply_price = data.get('supply_price', 0)
    collateral_value = supply_amount * supply_price
    max_possible_borrow = collateral_value * (value / 100)
    
    await msg.answer(
        f"✅ <b>Maximum LTV: {value}%</b>\n"
        f"💰 Макс. возможный займ: <b>{format_currency(max_possible_borrow)}</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Введите <b>Liquidation Threshold (LT)</b> в %\n"
        "(например: 75)"
    )
    await state.set_state(Calc.lt)


@dp.message(Calc.lt)
async def process_lt(msg: types.Message, state: FSMContext):
    """Liquidation Threshold - ВТОРОЙ параметр"""
    valid, value, error = validate_number(msg.text, min_val=0, max_val=100)
    if not valid:
        await msg.answer(f"❌ {error}\n\nLT должен быть 0-100%. Введите:")
        return
    
    data = await state.get_data()
    max_ltv = data.get('max_ltv', 0) * 100
    
    # Проверка: LT должен быть >= Max LTV
    if value < max_ltv:
        await msg.answer(
            f"❌ <b>Ошибка:</b> Liquidation Threshold ({value}%) должен быть "
            f"больше или равен Maximum LTV ({max_ltv:.0f}%)\n\n"
            "Введите корректное значение LT:"
        )
        return
    
    await state.update_data(lt=value / 100)
    
    await msg.answer(
        f"✅ <b>Liquidation Threshold: {value}%</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Выберите <b>режим расчета</b>:",
        reply_markup=mode_kb
    )
    await state.set_state(Calc.mode)


@dp.callback_query(F.data.startswith("mode_"))
async def process_mode(cb: types.CallbackQuery, state: FSMContext):
    """Режим расчета - ТРЕТИЙ выбор"""
    await cb.answer()
    mode = cb.data
    data = await state.get_data()
    
    supply_amount = data.get('supply_amount', 0)
    supply_price = data.get('supply_price', 0)
    collateral_value = supply_amount * supply_price
    max_ltv = data.get('max_ltv', 0)
    
    await state.update_data(mode=mode)
    
    if mode == "mode_ltv":
        await cb.message.edit_text(
            f"<b>🔢 Режим: Расчет по LTV</b>\n\n"
            f"Стоимость залога: {format_currency(collateral_value)}\n"
            f"Maximum LTV: {max_ltv * 100:.0f}%\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Введите <b>LTV</b> в %\n"
            "(например: 50)"
        )
        await state.set_state(Calc.ltv)
    else:
        # Рассчитываем максимально возможную сумму займа
        max_possible_borrow = collateral_value * max_ltv
        
        await cb.message.edit_text(
            f"<b>💵 Режим: Расчет по сумме займа</b>\n\n"
            f"Стоимость залога: {format_currency(collateral_value)}\n"
            f"Maximum LTV: {max_ltv * 100:.0f}%\n"
            f"<b>Макс. возможный займ: {format_currency(max_possible_borrow)}</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Введите <b>сумму займа</b> в USD\n"
            f"(максимум: {format_currency(max_possible_borrow)})"
        )
        await state.set_state(Calc.borrow)


@dp.message(Calc.ltv)
async def process_ltv(msg: types.Message, state: FSMContext):
    """LTV для расчета"""
    valid, value, error = validate_number(msg.text, min_val=0, max_val=100)
    if not valid:
        await msg.answer(f"❌ {error}\n\nLTV должен быть 0-100%. Введите:")
        return
    
    data = await state.get_data()
    max_ltv = data.get('max_ltv', 0) * 100
    
    # Проверка: LTV должен быть <= Max LTV
    if value > max_ltv:
        await msg.answer(
            f"❌ <b>Ошибка:</b> LTV ({value}%) не может превышать "
            f"Maximum LTV ({max_ltv:.0f}%)\n\n"
            "Введите корректное значение:"
        )
        return
    
    await state.update_data(ltv=value / 100)
    
    # Переходим к расчету
    await calculate_position(msg, state)


@dp.message(Calc.borrow)
async def process_borrow(msg: types.Message, state: FSMContext):
    """Сумма займа"""
    valid, value, error = validate_number(msg.text, min_val=0)
    if not valid:
        await msg.answer(f"❌ {error}\n\nВведите сумму:")
        return
    
    data = await state.get_data()
    supply_amount = data.get('supply_amount', 0)
    supply_price = data.get('supply_price', 0)
    collateral_value = supply_amount * supply_price
    max_ltv = data.get('max_ltv', 0)
    max_borrow_allowed = collateral_value * max_ltv
    
    # Проверка: займ не должен превышать максимально возможный
    if value > max_borrow_allowed:
        await msg.answer(
            f"❌ <b>Ошибка:</b> Сумма займа ({format_currency(value)}) превышает "
            f"максимально возможный займ ({format_currency(max_borrow_allowed)}) "
            f"при Max LTV {max_ltv*100:.0f}%\n\n"
            "Введите корректную сумму:"
        )
        return
    
    await state.update_data(borrow=value)
    
    # Переходим к расчету
    await calculate_position(msg, state)


# =============================================================================
# CALCULATION
# =============================================================================

async def calculate_position(msg: types.Message, state: FSMContext):
    """Финальный расчет"""
    try:
        data = await state.get_data()
        
        # Проверка данных
        required = ['supply_ticker', 'borrow_ticker', 'supply_amount', 
                   'supply_price', 'lt', 'max_ltv', 'mode']
        if not all(f in data for f in required):
            await msg.answer("❌ Недостаточно данных. Начните заново с /start")
            await state.clear()
            return
        
        supply_amt = data['supply_amount']
        price = data['supply_price']
        lt = data['lt']
        max_ltv = data['max_ltv']
        mode = data['mode']
        
        collateral = supply_amt * price
        
        # Расчет займа и LTV
        if mode == "mode_ltv":
            ltv = data.get('ltv')
            if ltv is None:
                await msg.answer("❌ Отсутствует LTV")
                await state.clear()
                return
            borrow = collateral * ltv
        else:
            borrow = data.get('borrow')
            if borrow is None:
                await msg.answer("❌ Отсутствует сумма займа")
                await state.clear()
                return
            ltv = borrow / collateral if collateral > 0 else 0
        
        ltv_percent = ltv * 100
        
        # Расчеты
        hf = calculate_health_factor(collateral, lt, borrow)
        liq_price = calculate_liquidation_price(borrow, supply_amt, lt)
        max_borrow = collateral * max_ltv
        buffer = ((price - liq_price) / price) * 100 if price > 0 else 0
        
        # Сценарии
        scenarios = []
        for drop in [10, 20, 30]:
            new_price = price * (1 - drop / 100)
            new_coll = supply_amt * new_price
            scen_hf = calculate_health_factor(new_coll, lt, borrow)
            scenarios.append((drop, scen_hf))
        
        # Собираем результаты
        calculations = {
            'supply_amt': supply_amt,
            'price': price,
            'collateral': collateral,
            'borrow': borrow,
            'ltv_percent': ltv_percent,
            'max_ltv_percent': max_ltv * 100,
            'lt': lt,
            'hf': hf,
            'liq_price': liq_price,
            'buffer': buffer,
            'max_borrow': max_borrow,
            'scenarios': scenarios
        }
        
        # Отправка результата
        result_message = build_result_message(data, calculations)
        
        await msg.answer("⏳ Формирую результаты...")
        await msg.answer(result_message)
        await msg.answer(
            "━━━━━━━━━━━━━━━━━━━━\n"
            "✅ Расчет завершен!\n\n"
            "/start - новый расчет"
        )
        
        await state.clear()
        
    except Exception as e:
        await msg.answer(f"❌ Ошибка: {str(e)}\n\nИспользуйте /start")
        await state.clear()


# =============================================================================
# FALLBACK & ERROR HANDLERS
# =============================================================================

@dp.message()
async def fallback_handler(msg: types.Message, state: FSMContext):
    """Обработчик неизвестных сообщений"""
    current_state = await state.get_state()
    if current_state:
        await msg.answer("⚠️ Следуйте инструкциям или используйте /reset")
    else:
        await msg.answer("👋 Привет! Используйте /start для начала расчета")


@dp.error()
async def error_handler(event, exception):
    """Глобальный обработчик ошибок"""
    print(f"❌ Ошибка: {exception}")
    return True


# =============================================================================
# STARTUP & SHUTDOWN
# =============================================================================

async def on_startup():
    print("\n" + "=" * 70)
    print("🚀 DeFi Risk.calc v2.1")
    print("=" * 70)
    
    bot_info = await bot.get_me()
    print(f"✅ Бот: @{bot_info.username}")
    
    test_price = await price_fetcher.get_price_usd("BTC")
    if test_price:
        print(f"✅ CoinGecko работает (BTC: ${test_price:,.2f})")
        print(f"✅ Автоцены: {len(price_fetcher.get_supported_symbols())} монет")
    
    print("✅ Новый порядок: Max LTV → LT → режим расчета")
    print("=" * 70)
    print("✅ БОТ ГОТОВ")
    print("=" * 70 + "\n")


async def on_shutdown():
    await price_fetcher.close()
    await bot.session.close()
    print("\n👋 Бот остановлен")


async def main():
    try:
        await on_startup()
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except KeyboardInterrupt:
        print("\n⚠️ Остановка...")
    finally:
        await on_shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 До свидания!")
