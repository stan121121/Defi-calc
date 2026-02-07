import asyncio
import os
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.strategy import FSMStrategy
from typing import Tuple, Optional, Dict, List
import aiohttp
from datetime import datetime, timedelta
from collections import deque
import json

# ════════════════════════════════════════════════════════════════════════════
# ⚙️  НАСТРОЙКА ЛОГИРОВАНИЯ (ДЛЯ RAILWAY)
# ════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),  # Для Railway логов
        logging.FileHandler('bot.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════════════════
# 🔐 КОНФИГУРАЦИЯ (ЧЕРЕЗ ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ)
# ════════════════════════════════════════════════════════════════════════════

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    logger.error("❌ Не установлен токен бота!")
    logger.info("📝 На Railway добавьте переменную окружения BOT_TOKEN")
    logger.info("📝 Локально: создайте .env файл с BOT_TOKEN=ваш_токен")
    raise ValueError("Токен бота не найден")

# API ключи (опционально)
CRYPTORANK_API_KEY = os.getenv("CRYPTORANK_API_KEY", "")

# ════════════════════════════════════════════════════════════════════════════
# 🤖 ИНИЦИАЛИЗАЦИЯ БОТА И ДИСПЕТЧЕРА
# ════════════════════════════════════════════════════════════════════════════

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
storage = MemoryStorage()
dp = Dispatcher(storage=storage, fsm_strategy=FSMStrategy.USER_IN_CHAT)

# ════════════════════════════════════════════════════════════════════════════
# 📊 CRYPTORANK API FETCHER
# ════════════════════════════════════════════════════════════════════════════

class CryptoRankPriceFetcher:
    """Получение цен через CryptoRank API с кэшированием"""
    
    BASE_URL = "https://api.cryptorank.io/v1"
    
    # Маппинг популярных тикеров (CryptoRank использует свои символы)
    # CryptoRank поддерживает тысячи монет, этот список для примера
    SYMBOL_MAPPING = {
        "ETH": "ETH",
        "BTC": "BTC",
        "SOL": "SOL",
        "USDC": "USDC",
        "USDT": "USDT",
        "DAI": "DAI",
        "BUSD": "BUSD",
        "BNB": "BNB",
        "ADA": "ADA",
        "DOT": "DOT",
        "AVAX": "AVAX",
        "MATIC": "MATIC",
        "LINK": "LINK",
        "UNI": "UNI",
        "ATOM": "ATOM",
        "XRP": "XRP",
        "LTC": "LTC",
        "DOGE": "DOGE",
        "SHIB": "SHIB",
        "AAVE": "AAVE",
    }
    
    def __init__(self, api_key: str = "", cache_ttl: int = 300, max_requests_per_minute: int = 30):
        self._cache: Dict[str, Tuple[float, datetime]] = {}
        self._cache_ttl = timedelta(seconds=cache_ttl)
        self._session: Optional[aiohttp.ClientSession] = None
        self._max_requests_per_minute = max_requests_per_minute
        self._request_times = deque(maxlen=max_requests_per_minute)
        self._rate_limit_lock = asyncio.Lock()
        self._api_key = api_key
        self._stats = {"total_requests": 0, "cache_hits": 0, "api_calls": 0, "errors": 0}
    
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=10)
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
        total = self._stats["total_requests"]
        cache_hits = self._stats["cache_hits"]
        cache_hit_rate = (cache_hits / total * 100) if total > 0 else 0
        return {
            **self._stats,
            "cache_hit_rate": f"{cache_hit_rate:.1f}%",
            "cache_size": len(self._cache),
            "has_api_key": bool(self._api_key)
        }
    
    async def get_price_usd(self, symbol: str, use_cache: bool = True) -> Optional[float]:
        """Получение цены в USD через CryptoRank API"""
        symbol = symbol.upper().strip()
        self._stats["total_requests"] += 1
        
        if use_cache:
            cached_price = self._get_from_cache(symbol)
            if cached_price is not None:
                return cached_price
        
        try:
            await self._wait_for_rate_limit()
            session = await self._get_session()
            self._stats["api_calls"] += 1
            
            # CryptoRank API endpoint для получения информации о валюте
            url = f"{self.BASE_URL}/currencies/{symbol}"
            params = {"api_key": self._api_key} if self._api_key else {}
            
            async with session.get(url, params=params) as response:
                if response.status == 429:  # Rate limit
                    retry_after = int(response.headers.get('Retry-After', '30'))
                    await asyncio.sleep(retry_after)
                    return await self.get_price_usd(symbol, use_cache=False)
                
                if response.status == 404:
                    # Если не нашли по символу, пробуем через поиск
                    return await self._search_price(symbol, use_cache)
                
                response.raise_for_status()
                data = await response.json()
                
                if data.get("status") and data["status"].get("error_code") == 0:
                    currency_data = data.get("data", {})
                    if currency_data:
                        # CryptoRank возвращает цену в USD
                        price = currency_data.get("price", {}).get("USD")
                        if price is not None:
                            if use_cache:
                                self._save_to_cache(symbol, float(price))
                            return float(price)
            
            # Если не получили цену, пробуем альтернативный endpoint
            return await self._get_price_from_tickers(symbol, use_cache)
            
        except aiohttp.ClientError as e:
            self._stats["errors"] += 1
            logger.error(f"❌ Ошибка CryptoRank API для {symbol}: {e}")
            return None
        except Exception as e:
            self._stats["errors"] += 1
            logger.error(f"❌ Неожиданная ошибка для {symbol}: {e}")
            return None
    
    async def _search_price(self, symbol: str, use_cache: bool) -> Optional[float]:
        """Поиск цены через поисковой endpoint"""
        try:
            session = await self._get_session()
            url = f"{self.BASE_URL}/search"
            params = {"query": symbol, "limit": 1}
            if self._api_key:
                params["api_key"] = self._api_key
            
            async with session.get(url, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get("data") and data["data"].get("currencies"):
                        currency = data["data"]["currencies"][0]
                        price = currency.get("price", {}).get("USD")
                        if price is not None:
                            if use_cache:
                                self._save_to_cache(symbol, float(price))
                            return float(price)
        except Exception:
            pass
        return None
    
    async def _get_price_from_tickers(self, symbol: str, use_cache: bool) -> Optional[float]:
        """Получение цены через endpoint тикеров"""
        try:
            session = await self._get_session()
            url = f"{self.BASE_URL}/currencies/{symbol}/prices/latest"
            params = {}
            if self._api_key:
                params["api_key"] = self._api_key
            
            async with session.get(url, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    price = data.get("data", {}).get("USD")
                    if price is not None:
                        if use_cache:
                            self._save_to_cache(symbol, float(price))
                        return float(price)
        except Exception:
            pass
        return None
    
    @classmethod
    def is_supported(cls, symbol: str) -> bool:
        """Проверка поддержки символа (CryptoRank поддерживает тысячи монет)"""
        symbol = symbol.upper().strip()
        # CryptoRank поддерживает большинство популярных монет
        # Для точной проверки нужен API запрос
        return len(symbol) <= 10 and symbol.isalnum()
    
    @classmethod
    def get_supported_symbols(cls) -> List[str]:
        """Возвращает примерный список поддерживаемых символов"""
        return sorted(cls.SYMBOL_MAPPING.keys())

# ════════════════════════════════════════════════════════════════════════════
# 📊 COINGECKO API FETCHER (ОРИГИНАЛЬНЫЙ КОД С УЛУЧШЕНИЯМИ)
# ════════════════════════════════════════════════════════════════════════════

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
        self._stats = {"total_requests": 0, "cache_hits": 0, "api_calls": 0, "errors": 0}
    
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
        return {
            **self._stats, 
            "cache_hit_rate": f"{cache_hit_rate:.1f}%", 
            "cache_size": len(self._cache)
        }
    
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
            self._stats["errors"] += 1
            logger.error(f"❌ Ошибка CoinGecko для {symbol}: {e}")
            return None
    
    @classmethod
    def is_supported(cls, symbol: str) -> bool:
        return symbol.upper().strip() in cls.COINGECKO_IDS
    
    @classmethod
    def get_supported_symbols(cls) -> List[str]:
        return sorted(cls.COINGECKO_IDS.keys())

# ════════════════════════════════════════════════════════════════════════════
# 🌐 УНИВЕРСАЛЬНЫЙ PRICE MANAGER
# ════════════════════════════════════════════════════════════════════════════

class PriceManager:
    """Менеджер для получения цен из разных источников"""
    
    def __init__(self):
        self.coingecko = CoinGeckoPriceFetcher(cache_ttl=300, max_requests_per_minute=5)
        self.cryptorank = CryptoRankPriceFetcher(
            api_key=CRYPTORANK_API_KEY,
            cache_ttl=300,
            max_requests_per_minute=30
        )
        self._preferred_source = "coingecko"  # coingecko, cryptorank, auto
    
    async def close(self):
        await self.coingecko.close()
        await self.cryptorank.close()
    
    def set_preferred_source(self, source: str):
        if source in ["coingecko", "cryptorank", "auto"]:
            self._preferred_source = source
    
    async def get_price_usd(self, symbol: str, source: str = "auto") -> Tuple[Optional[float], str, str]:
        """
        Получение цены из указанного источника
        Возвращает: (цена, источник, сообщение_об_ошибке)
        """
        symbol = symbol.upper().strip()
        
        # Определяем источник
        if source == "auto":
            use_source = self._preferred_source
        else:
            use_source = source
        
        price = None
        error_msg = ""
        
        if use_source == "coingecko" or (use_source == "auto" and self._preferred_source == "coingecko"):
            if self.coingecko.is_supported(symbol):
                price = await self.coingecko.get_price_usd(symbol)
                if price is not None:
                    return price, "coingecko", ""
                error_msg = "CoinGecko не вернул цену"
            else:
                error_msg = "CoinGecko не поддерживает этот тикер"
        
        # Пробуем CryptoRank если CoinGecko не сработал
        if price is None:
            if self.cryptorank.is_supported(symbol):
                price = await self.cryptorank.get_price_usd(symbol)
                if price is not None:
                    return price, "cryptorank", ""
                error_msg = "CryptoRank не вернул цену"
            else:
                if not error_msg:
                    error_msg = "CryptoRank не поддерживает этот тикер"
        
        return None, "", error_msg
    
    async def get_price_with_fallback(self, symbol: str) -> Tuple[Optional[float], str]:
        """Получение цены с автоматическим переключением между источниками"""
        symbol = symbol.upper().strip()
        
        # Сначала пробуем CoinGecko
        if self.coingecko.is_supported(symbol):
            price = await self.coingecko.get_price_usd(symbol)
            if price is not None:
                return price, "coingecko"
        
        # Затем CryptoRank
        if self.cryptorank.is_supported(symbol):
            price = await self.cryptorank.get_price_usd(symbol)
            if price is not None:
                return price, "cryptorank"
        
        return None, ""
    
    def get_supported_symbols(self) -> Dict[str, List[str]]:
        """Получение списка поддерживаемых символов из всех источников"""
        return {
            "coingecko": self.coingecko.get_supported_symbols(),
            "cryptorank": self.cryptorank.get_supported_symbols()
        }
    
    def get_stats(self) -> Dict[str, dict]:
        """Статистика по всем источникам"""
        return {
            "coingecko": self.coingecko.get_stats(),
            "cryptorank": self.cryptorank.get_stats()
        }

# ════════════════════════════════════════════════════════════════════════════
# 🚀 ИНИЦИАЛИЗАЦИЯ
# ════════════════════════════════════════════════════════════════════════════

price_manager = PriceManager()

# ════════════════════════════════════════════════════════════════════════════
# 📝 СОСТОЯНИЯ FSM (БЕЗ ИЗМЕНЕНИЙ)
# ════════════════════════════════════════════════════════════════════════════

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

# ════════════════════════════════════════════════════════════════════════════
# ⌨️  КЛАВИАТУРЫ
# ════════════════════════════════════════════════════════════════════════════

mode_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🔢 По LTV", callback_data="mode_ltv")],
    [InlineKeyboardButton(text="💵 По сумме займа", callback_data="mode_borrow")]
])

# =============================================================================
# 📊 НОВЫЕ КОМАНДЫ ДЛЯ УПРАВЛЕНИЯ ИСТОЧНИКАМИ ЦЕН
# =============================================================================

@dp.message(Command("sources"))
async def sources_cmd(msg: types.Message):
    """Информация об источниках цен"""
    stats = price_manager.get_stats()
    supported = price_manager.get_supported_symbols()
    
    text = (
        "<b>📊 ИСТОЧНИКИ ЦЕН</b>\n\n"
        f"<b>CoinGecko:</b>\n"
        f"• Запросов: {stats['coingecko']['total_requests']}\n"
        f"• API вызовов: {stats['coingecko']['api_calls']}\n"
        f"• Кэш: {stats['coingecko']['cache_hit_rate']}\n"
        f"• Поддерживает: {len(supported['coingecko'])} монет\n\n"
        
        f"<b>CryptoRank:</b>\n"
        f"• Запросов: {stats['cryptorank']['total_requests']}\n"
        f"• API вызовов: {stats['cryptorank']['api_calls']}\n"
        f"• Кэш: {stats['cryptorank']['cache_hit_rate']}\n"
        f"• API ключ: {'✅ есть' if stats['cryptorank']['has_api_key'] else '❌ нет'}\n"
        f"• Поддерживает: тысячи монет\n\n"
        
        "<b>💡 Использование:</b>\n"
        "Бот автоматически выбирает лучший источник.\n"
        "CryptoRank используется как fallback для CoinGecko.\n\n"
        
        "<b>⚙️ Настройка:</b>\n"
        "Для CryptoRank API добавьте переменную окружения:\n"
        "<code>CRYPTORANK_API_KEY=ваш_ключ</code>"
    )
    
    await msg.answer(text)

@dp.message(Command("cryptorank"))
async def cryptorank_cmd(msg: types.Message):
    """Информация о CryptoRank API"""
    has_key = bool(CRYPTORANK_API_KEY)
    
    text = (
        "<b>🔑 CRYPTORANK API</b>\n\n"
        f"<b>Статус:</b> {'✅ Настроен' if has_key else '⚠️ Без API ключа'}\n\n"
        
        "<b>📈 Возможности:</b>\n"
        "• Цены тысяч криптовалют\n"
        "• Рыночные данные\n"
        "• Исторические данные\n"
        "• Лучшие rate limits\n\n"
        
        "<b>🔧 Настройка:</b>\n"
        "1. Получите API ключ на cryptorank.io\n"
        "2. На Railway добавьте переменную:\n"
        "   <code>CRYPTORANK_API_KEY=ваш_ключ</code>\n"
        "3. Перезапустите бота\n\n"
        
        "<b>💡 Примечание:</b>\n"
        "Без ключа работают ограниченные запросы."
    )
    
    await msg.answer(text)

# =============================================================================
# 🚀 ОБНОВЛЕННЫЙ STATE HANDLER ДЛЯ ЦЕН
# =============================================================================

@dp.message(Calc.supply_amount)
async def process_supply_amount(msg: types.Message, state: FSMContext):
    """Количество залога с поддержкой обоих API"""
    valid, value, error = validate_number(msg.text, min_val=0.000001)
    if not valid:
        await msg.answer(f"❌ {error}\n\nВведите количество:")
        return
    
    await state.update_data(supply_amount=value)
    data = await state.get_data()
    ticker = data['supply_ticker']
    
    # Получение цены через универсальный менеджер
    await msg.answer(f"✅ Количество: {value:.6f}\n\n⏳ Получаю цену {ticker}...")
    
    price, source, error_msg = await price_manager.get_price_usd(ticker, source="auto")
    
    if price is not None:
        await state.update_data(supply_price=price, supply_price_source=source)
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
        
        source_emoji = "🌐" if source == "coingecko" else "📊"
        source_name = "CoinGecko" if source == "coingecko" else "CryptoRank"
        
        await msg.answer(
            f"✅ Цена ({source_emoji} {source_name}): <b>{price_str}</b>\n"
            f"💰 Стоимость залога: <b>{format_currency(collateral_value)}</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Введите <b>Maximum LTV</b> в %\n"
            "(например: 65)"
        )
        await state.set_state(Calc.max_ltv)
    else:
        # Если оба API не сработали, просим ручной ввод
        await msg.answer(
            f"❌ Не удалось получить цену автоматически\n"
            f"Ошибка: {error_msg}\n\n"
            f"Введите <b>цену {ticker}</b> в USD вручную:"
        )
        await state.set_state(Calc.supply_price_manual)

# =============================================================================
# 🚀 ФУНКЦИИ ДЛЯ ЗАПУСКА НА RAILWAY
# =============================================================================

async def on_startup():
    """Запуск при старте бота"""
    print("\n" + "=" * 70)
    print("🚀 DeFi Position Calculator Bot v2.2")
    print("=" * 70)
    
    bot_info = await bot.get_me()
    print(f"✅ Бот: @{bot_info.username}")
    print(f"✅ Режим: Railway Deploy Ready")
    
    # Проверка API
    print("\n🔧 Проверка источников цен...")
    
    # Проверка CoinGecko
    test_price, source = await price_manager.get_price_with_fallback("BTC")
    if test_price:
        print(f"✅ {source.upper()}: BTC = ${test_price:,.2f}")
    else:
        print("⚠️  CoinGecko недоступен")
    
    # Проверка CryptoRank
    if CRYPTORANK_API_KEY:
        print(f"✅ CryptoRank API ключ: установлен")
    else:
        print("⚠️  CryptoRank API ключ: не установлен (ограниченный доступ)")
    
    # Показываем статистику
    stats = price_manager.get_stats()
    print(f"📊 CoinGecko кэш: {stats['coingecko']['cache_size']} записей")
    print(f"📊 CryptoRank кэш: {stats['cryptorank']['cache_size']} записей")
    
    print("\n✅ БОТ ГОТОВ К РАБОТЕ")
    print("=" * 70)
    print("💡 Команды: /start, /sources, /cryptorank, /stats, /help")
    print("=" * 70 + "\n")

async def on_shutdown():
    """Очистка при завершении"""
    await price_manager.close()
    await bot.session.close()
    print("\n👋 Бот остановлен, ресурсы очищены")

# =============================================================================
# 🚀 ГЛАВНАЯ ФУНКЦИЯ ДЛЯ RAILWAY
# =============================================================================

async def main():
    """Основная функция запуска для Railway"""
    try:
        # Инициализация
        await on_startup()
        
        # Удаление вебхука (если был)
        await bot.delete_webhook(drop_pending_updates=True)
        
        # Запуск поллинга
        logger.info("Бот запущен в режиме polling для Railway")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        
    except KeyboardInterrupt:
        print("\n⚠️  Остановка по запросу пользователя...")
    except Exception as e:
        logger.error(f"💥 Критическая ошибка: {e}")
        raise
    finally:
        await on_shutdown()

# =============================================================================
# 🚀 ТОЧКА ВХОДА ДЛЯ RAILWAY
# =============================================================================

if __name__ == "__main__":
    # Это важно для Railway - запуск через asyncio.run
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 До свидания!")
    except Exception as e:
        logger.error(f"💥 Фатальная ошибка при запуске: {e}")
