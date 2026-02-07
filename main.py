"""
=============================================================================
DeFi Position Calculator Bot - Полная версия
=============================================================================

Возможности:
✅ Автоматическое получение цен через CoinGecko API
✅ Ручной ввод цены для любых токенов
✅ Rate limiting для защиты от 429 ошибок
✅ Кэширование цен (5 минут)
✅ Расчет Health Factor, цены ликвидации, сценариев
✅ Два режима: по LTV или по сумме займа

Автор: DeFi Calculator Team
Версия: 2.0
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
# PRICE FETCHER - Получение цен с CoinGecko
# =============================================================================

class CoinGeckoPriceFetcher:
    """
    Класс для получения цен криптовалют через CoinGecko API
    с кэшированием, rate limiting и retry механизмом
    """
    
    # Поддерживаемые монеты для автоматического получения цен
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
    
    def __init__(
        self, 
        cache_ttl: int = 300,  # 5 минут кэш
        max_requests_per_minute: int = 5  # Консервативный лимит
    ):
        """
        Инициализация price fetcher
        
        Args:
            cache_ttl: Время жизни кэша в секундах
            max_requests_per_minute: Максимум запросов к API в минуту
        """
        self._cache: Dict[str, Tuple[float, datetime]] = {}
        self._cache_ttl = timedelta(seconds=cache_ttl)
        self._session: Optional[aiohttp.ClientSession] = None
        
        # Rate limiting
        self._max_requests_per_minute = max_requests_per_minute
        self._request_times = deque(maxlen=max_requests_per_minute)
        self._rate_limit_lock = asyncio.Lock()
        
        # Статистика
        self._stats = {
            "total_requests": 0,
            "cache_hits": 0,
            "api_calls": 0,
            "rate_limit_waits": 0,
            "errors": 0
        }
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Получает или создает HTTP сессию"""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=15)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session
    
    async def close(self):
        """Закрывает HTTP сессию"""
        if self._session and not self._session.closed:
            await self._session.close()
    
    async def _wait_for_rate_limit(self):
        """Ожидает если достигнут лимит запросов"""
        async with self._rate_limit_lock:
            now = datetime.now()
            
            # Удаляем старые запросы (старше 1 минуты)
            while self._request_times and (now - self._request_times[0]).total_seconds() > 60:
                self._request_times.popleft()
            
            # Если достигнут лимит, ждем
            if len(self._request_times) >= self._max_requests_per_minute:
                oldest_request = self._request_times[0]
                wait_time = 60 - (now - oldest_request).total_seconds()
                
                if wait_time > 0:
                    self._stats["rate_limit_waits"] += 1
                    print(f"⏳ Rate limit: ожидание {wait_time:.1f}s...")
                    await asyncio.sleep(wait_time + 0.5)
            
            # Регистрируем новый запрос
            self._request_times.append(now)
    
    def _get_from_cache(self, symbol: str) -> Optional[float]:
        """Получает цену из кэша"""
        if symbol in self._cache:
            price, timestamp = self._cache[symbol]
            if datetime.now() - timestamp < self._cache_ttl:
                self._stats["cache_hits"] += 1
                return price
        return None
    
    def _save_to_cache(self, symbol: str, price: float):
        """Сохраняет цену в кэш"""
        self._cache[symbol] = (price, datetime.now())
    
    def clear_cache(self):
        """Очищает кэш"""
        self._cache.clear()
    
    def get_stats(self) -> dict:
        """Возвращает статистику использования"""
        cache_hit_rate = (
            self._stats["cache_hits"] / self._stats["total_requests"] * 100 
            if self._stats["total_requests"] > 0 else 0
        )
        return {
            **self._stats,
            "cache_hit_rate": f"{cache_hit_rate:.1f}%",
            "cache_size": len(self._cache)
        }
    
    async def get_price_usd(
        self, 
        symbol: str, 
        use_cache: bool = True
    ) -> Optional[float]:
        """
        Получает цену криптовалюты в USD
        
        Args:
            symbol: Тикер (ETH, BTC и т.д.)
            use_cache: Использовать ли кэш
            
        Returns:
            Цена в USD или None если ошибка
        """
        symbol = symbol.upper().strip()
        self._stats["total_requests"] += 1
        
        # Проверяем кэш
        if use_cache:
            cached_price = self._get_from_cache(symbol)
            if cached_price is not None:
                return cached_price
        
        # Проверяем поддержку
        if symbol not in self.COINGECKO_IDS:
            return None
        
        url = f"{self.BASE_URL}/simple/price"
        params = {
            "ids": self.COINGECKO_IDS[symbol],
            "vs_currencies": "usd"
        }
        
        try:
            await self._wait_for_rate_limit()
            session = await self._get_session()
            self._stats["api_calls"] += 1
            
            async with session.get(url, params=params) as response:
                # Обработка 429
                if response.status == 429:
                    retry_after = int(response.headers.get('Retry-After', '60'))
                    print(f"⚠️ 429 Too Many Requests. Ожидание {retry_after}s...")
                    await asyncio.sleep(retry_after)
                    return await self.get_price_usd(symbol, use_cache=False)
                
                response.raise_for_status()
                data = await response.json()
                
                coin_id = self.COINGECKO_IDS[symbol]
                if coin_id not in data or "usd" not in data[coin_id]:
                    return None
                
                price = data[coin_id]["usd"]
                
                # Сохраняем в кэш
                if use_cache:
                    self._save_to_cache(symbol, price)
                
                return price
                
        except Exception as e:
            print(f"❌ Ошибка получения цены {symbol}: {e}")
            self._stats["errors"] += 1
            return None
    
    @classmethod
    def is_supported(cls, symbol: str) -> bool:
        """Проверяет поддержку автоматического получения цены"""
        return symbol.upper().strip() in cls.COINGECKO_IDS
    
    @classmethod
    def get_supported_symbols(cls) -> list:
        """Возвращает список поддерживаемых тикеров"""
        return sorted(cls.COINGECKO_IDS.keys())


# =============================================================================
# CONFIGURATION
# =============================================================================

TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise ValueError(
        "❌ Не установлен токен бота!\n"
        "Создайте файл .env с строкой: BOT_TOKEN=ваш_токен\n"
        "Или установите переменную окружения"
    )

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=MemoryStorage(), fsm_strategy=FSMStrategy.USER_IN_CHAT)

# Глобальный price fetcher
price_fetcher = CoinGeckoPriceFetcher(
    cache_ttl=300,  # 5 минут
    max_requests_per_minute=5  # Консервативно
)


# =============================================================================
# FSM STATES
# =============================================================================

class Calc(StatesGroup):
    """Состояния для расчета позиции"""
    supply_ticker = State()           # Тикер залогового актива
    borrow_ticker = State()           # Тикер заимствуемого актива
    supply_amount = State()           # Количество залога
    supply_price_manual = State()    # Ручной ввод цены залога
    borrow_price_manual = State()    # Ручной ввод цены займа (не используется пока)
    mode = State()                    # Режим расчета (LTV/сумма)
    ltv = State()                     # LTV
    borrow = State()                  # Сумма займа
    lt = State()                      # Liquidation Threshold
    max_ltv = State()                 # Maximum LTV


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

def validate_number(
    text: str, 
    min_val: float = 0, 
    max_val: Optional[float] = None
) -> Tuple[bool, float, str]:
    """
    Валидация числового ввода
    
    Returns:
        (валидно, значение, сообщение об ошибке)
    """
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
    """
    Валидация тикера
    
    Returns:
        (валидно, тикер, сообщение об ошибке)
    """
    ticker = text.upper().strip()
    
    if len(ticker) > max_length:
        return False, "", f"Тикер слишком длинный (максимум {max_length} символов)"
    
    if not ticker.isalnum():
        return False, "", "Тикер должен содержать только буквы и цифры"
    
    return True, ticker, ""


def format_currency(value: float) -> str:
    """Форматирует денежные значения"""
    if value >= 1_000_000:
        return f"${value/1_000_000:.2f}M"
    elif value >= 1_000:
        return f"${value/1_000:.1f}K"
    else:
        return f"${value:.2f}"


def format_number(value: float, decimals: int = 2) -> str:
    """Форматирует числа"""
    if value == float('inf'):
        return "∞"
    return f"{value:.{decimals}f}"


def calculate_health_factor(collateral: float, lt: float, borrow: float) -> float:
    """Рассчитывает Health Factor"""
    if borrow <= 0:
        return float('inf')
    return (collateral * lt) / borrow


def calculate_liquidation_price(borrow: float, supply_amount: float, lt: float) -> float:
    """Рассчитывает цену ликвидации"""
    denominator = supply_amount * lt
    if denominator <= 0:
        return 0
    return borrow / denominator


def get_position_status(hf: float) -> Tuple[str, str]:
    """
    Определяет статус позиции по Health Factor
    
    Returns:
        (статус с эмодзи, эмодзи)
    """
    if hf <= 1.0:
        return "🔴 ЛИКВИДАЦИЯ", "🔴"
    elif hf < 1.3:
        return "🟡 ВНИМАНИЕ", "🟡"
    elif hf < 2.0:
        return "🟢 БЕЗОПАСНО", "🟢"
    else:
        return "🔵 ОЧЕНЬ БЕЗОПАСНО", "🔵"


def build_result_message(data: dict, calculations: dict) -> str:
    """
    Формирует итоговое сообщение с результатами
    
    Args:
        data: Данные из FSM state
        calculations: Результаты расчетов
    """
    status, emoji = get_position_status(calculations['hf'])
    price_source = data.get('supply_price_source', 'manual')
    price_info = "CoinGecko" if price_source == "auto" else "ручной ввод"
    
    result = (
        f"<b>{emoji} РАСЧЕТ ПОЗИЦИИ</b>\n"
        f"Статус: <b>{status}</b>\n\n"
        
        f"<b>💎 ЗАЛОГ:</b>\n"
        f"• Актив: <b>{data['supply_ticker']}</b>\n"
        f"• Количество: {calculations['supply_amt']:.6f}\n"
        f"• Цена ({price_info}): ${calculations['price']:,.2f}\n"
        f"• Стоимость: <b>{format_currency(calculations['collateral'])}</b>\n\n"
        
        f"<b>💰 ЗАЙМ:</b>\n"
        f"• Актив: <b>{data['borrow_ticker']}</b>\n"
        f"• Сумма: <b>{format_currency(calculations['borrow'])}</b>\n\n"
        
        f"<b>⚙️ ПАРАМЕТРЫ:</b>\n"
        f"• Current LTV: <b>{calculations['ltv_percent']:.2f}%</b>\n"
        f"• Maximum LTV: {calculations['max_ltv_percent']}%\n"
        f"• Liquidation Threshold: {calculations['lt']*100:.1f}%\n\n"
        
        f"<b>📊 РИСКИ:</b>\n"
        f"• Health Factor: <b>{format_number(calculations['hf'], 2)}</b>\n"
        f"• Цена ликвидации: <b>${calculations['liq_price']:.2f}</b>\n"
        f"• Буфер безопасности: <b>{calculations['buffer']:.1f}%</b>\n"
        f"• Макс. возможный займ: {format_currency(calculations['max_borrow'])}\n\n"
        
        f"<b>📉 СЦЕНАРИИ (падение цены):</b>\n"
    )
    
    for drop, scen_hf in calculations['scenarios']:
        new_price = calculations['price'] * (1 - drop / 100)
        result += f"• -{drop}% (${new_price:.2f}) → HF: {format_number(scen_hf, 2)}\n"
    
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
    """Начало работы с ботом"""
    await state.clear()
    
    supported = price_fetcher.get_supported_symbols()
    supported_preview = ", ".join(supported[:10])
    
    await msg.answer(
        "🤖 <b>DeFi Position Calculator</b>\n"
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


@dp.message(Command("reset", "cancel", "отмена"))
async def reset_cmd(msg: types.Message, state: FSMContext):
    """Сброс текущего расчета"""
    current_state = await state.get_state()
    await state.clear()
    
    if current_state:
        await msg.answer(
            "✅ <b>Расчет сброшен</b>\n\n"
            "Используйте /start для нового расчета"
        )
    else:
        await msg.answer(
            "ℹ️ Нет активного расчета\n\n"
            "Используйте /start для начала"
        )


@dp.message(Command("help", "помощь"))
async def help_cmd(msg: types.Message):
    """Помощь по использованию бота"""
    await msg.answer(
        "<b>📖 Справка по боту</b>\n\n"
        
        "<b>🎯 Что делает бот:</b>\n"
        "• Рассчитывает Health Factor позиции\n"
        "• Определяет цену ликвидации\n"
        "• Показывает максимальный займ\n"
        "• Моделирует сценарии падения цены\n\n"
        
        "<b>⌨️ Команды:</b>\n"
        "• /start - начать новый расчет\n"
        "• /reset - сбросить текущий расчет\n"
        "• /supported - список монет с авто-ценами\n"
        "• /stats - статистика использования API\n"
        "• /help - эта справка\n\n"
        
        "<b>📊 Термины:</b>\n"
        "• <b>LTV</b> (Loan-to-Value) - отношение займа к залогу\n"
        "• <b>LT</b> (Liquidation Threshold) - порог ликвидации\n"
        "• <b>HF</b> (Health Factor) - фактор здоровья позиции\n"
        "  • HF > 2.0 - очень безопасно 🔵\n"
        "  • HF 1.3-2.0 - безопасно 🟢\n"
        "  • HF 1.0-1.3 - внимание! 🟡\n"
        "  • HF < 1.0 - ликвидация! 🔴\n\n"
        
        "<b>💡 Способы получения цен:</b>\n"
        "• <b>Автоматически</b> - для поддерживаемых монет\n"
        "• <b>Вручную</b> - для любых других токенов\n\n"
        
        "❓ Вопросы? Просто начните с /start"
    )


@dp.message(Command("supported"))
async def supported_cmd(msg: types.Message):
    """Показать список поддерживаемых монет"""
    supported = price_fetcher.get_supported_symbols()
    
    # Разбиваем на колонки
    cols = 4
    rows = []
    for i in range(0, len(supported), cols):
        row = " | ".join(f"<code>{coin}</code>" for coin in supported[i:i+cols])
        rows.append(row)
    
    await msg.answer(
        f"<b>💎 Монеты с автоматическим получением цен</b>\n"
        f"<i>Всего: {len(supported)}</i>\n\n"
        + "\n".join(rows) + 
        "\n\n💡 <i>Для всех остальных токенов можно ввести цену вручную</i>"
    )


@dp.message(Command("stats"))
async def stats_cmd(msg: types.Message):
    """Показать статистику использования API"""
    stats = price_fetcher.get_stats()
    
    await msg.answer(
        "<b>📊 Статистика использования API</b>\n\n"
        f"Всего запросов: {stats['total_requests']}\n"
        f"API вызовов: {stats['api_calls']}\n"
        f"Попаданий в кэш: {stats['cache_hits']}\n"
        f"Процент кэша: {stats['cache_hit_rate']}\n"
        f"Ожиданий rate limit: {stats['rate_limit_waits']}\n"
        f"Ошибок: {stats['errors']}\n"
        f"Размер кэша: {stats['cache_size']} монет"
    )


# =============================================================================
# STATE HANDLERS - Обработка ввода данных
# =============================================================================

@dp.message(Calc.supply_ticker)
async def process_supply_ticker(msg: types.Message, state: FSMContext):
    """Обработка тикера залогового актива"""
    valid, ticker, error = validate_ticker(msg.text)
    
    if not valid:
        await msg.answer(
            f"❌ <b>Ошибка:</b> {error}\n\n"
            "Пожалуйста, введите корректный тикер:"
        )
        return
    
    await state.update_data(supply_ticker=ticker)
    
    # Проверяем доступность автоматического получения цены
    is_supported = price_fetcher.is_supported(ticker)
    
    if is_supported:
        await msg.answer(
            f"✅ <b>Залоговый актив:</b> {ticker}\n"
            f"🌐 Автоматическое получение цены: доступно\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Введите <b>тикер заимствуемого актива</b>\n"
            "(например: USDC, DAI, USDT)"
        )
    else:
        await msg.answer(
            f"✅ <b>Залоговый актив:</b> {ticker}\n"
            f"✍️ Автоматическое получение цены: недоступно\n"
            f"💡 Вы сможете ввести цену вручную позже\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Введите <b>тикер заимствуемого актива</b>"
        )
    
    await state.set_state(Calc.borrow_ticker)


@dp.message(Calc.borrow_ticker)
async def process_borrow_ticker(msg: types.Message, state: FSMContext):
    """Обработка тикера заимствуемого актива"""
    valid, ticker, error = validate_ticker(msg.text)
    
    if not valid:
        await msg.answer(
            f"❌ <b>Ошибка:</b> {error}\n\n"
            "Пожалуйста, введите корректный тикер:"
        )
        return
    
    await state.update_data(borrow_ticker=ticker)
    data = await state.get_data()
    
    is_supported = price_fetcher.is_supported(ticker)
    
    if is_supported:
        await msg.answer(
            f"✅ <b>Заимствуемый актив:</b> {ticker}\n"
            f"🌐 Автоматическое получение цены: доступно\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"Введите <b>количество {data['supply_ticker']}</b>\n"
            "(например: 10 или 0.5)"
        )
    else:
        await msg.answer(
            f"✅ <b>Заимствуемый актив:</b> {ticker}\n"
            f"✍️ Автоматическое получение цены: недоступно\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"Введите <b>количество {data['supply_ticker']}</b>"
        )
    
    await state.set_state(Calc.supply_amount)


@dp.message(Calc.supply_amount)
async def process_supply_amount(msg: types.Message, state: FSMContext):
    """Обработка количества залогового актива"""
    valid, value, error = validate_number(msg.text, min_val=0.000001)
    
    if not valid:
        await msg.answer(
            f"❌ <b>Ошибка:</b> {error}\n\n"
            "Пожалуйста, введите корректное количество:"
        )
        return
    
    await state.update_data(supply_amount=value)
    data = await state.get_data()
    ticker = data['supply_ticker']
    
    # Пытаемся получить цену автоматически
    if price_fetcher.is_supported(ticker):
        await msg.answer(
            f"✅ <b>Количество {ticker}:</b> {value:.6f}\n\n"
            f"⏳ Получаю актуальную цену {ticker}..."
        )
        
        price = await price_fetcher.get_price_usd(ticker)
        
        if price is None:
            # Не удалось получить - переходим на ручной ввод
            await msg.answer(
                f"❌ Не удалось получить цену {ticker} автоматически\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"Введите <b>цену {ticker}</b> в USD вручную\n"
                "(например: 2500 или 0.05)"
            )
            await state.set_state(Calc.supply_price_manual)
            return
        
        # Успешно получили цену
        await state.update_data(supply_price=price, supply_price_source="auto")
        collateral_value = value * price
        
        await msg.answer(
            f"<b>📊 Информация о залоге</b>\n\n"
            f"Актив: <b>{ticker}</b>\n"
            f"Количество: {value:.6f}\n"
            f"Цена (CoinGecko): <b>${price:,.2f}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>💰 Стоимость залога: {format_currency(collateral_value)}</b>\n\n"
            "Выберите режим расчета:",
            reply_markup=mode_kb
        )
        await state.set_state(Calc.mode)
    else:
        # Ручной ввод цены
        await msg.answer(
            f"✅ <b>Количество {ticker}:</b> {value:.6f}\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"Введите <b>цену {ticker}</b> в USD\n"
            "(например: 2500 или 0.05)"
        )
        await state.set_state(Calc.supply_price_manual)


@dp.message(Calc.supply_price_manual)
async def process_supply_price_manual(msg: types.Message, state: FSMContext):
    """Обработка ручного ввода цены залогового актива"""
    valid, price, error = validate_number(msg.text, min_val=0.000001)
    
    if not valid:
        await msg.answer(
            f"❌ <b>Ошибка:</b> {error}\n\n"
            "Пожалуйста, введите корректную цену в USD:"
        )
        return
    
    data = await state.get_data()
    ticker = data['supply_ticker']
    amount = data['supply_amount']
    
    await state.update_data(supply_price=price, supply_price_source="manual")
    collateral_value = amount * price
    
    await msg.answer(
        f"<b>📊 Информация о залоге</b>\n\n"
        f"Актив: <b>{ticker}</b>\n"
        f"Количество: {amount:.6f}\n"
        f"Цена (ручной ввод): <b>${price:,.2f}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>💰 Стоимость залога: {format_currency(collateral_value)}</b>\n\n"
        "Выберите режим расчета:",
        reply_markup=mode_kb
    )
    await state.set_state(Calc.mode)


@dp.callback_query(F.data.startswith("mode_"))
async def process_mode(cb: types.CallbackQuery, state: FSMContext):
    """Обработка выбора режима расчета"""
    await cb.answer()
    
    mode = cb.data
    data = await state.get_data()
    
    supply_amount = data.get('supply_amount', 0)
    supply_price = data.get('supply_price', 0)
    collateral_value = supply_amount * supply_price
    
    await state.update_data(mode=mode)
    
    if mode == "mode_ltv":
        await cb.message.edit_text(
            f"<b>🔢 Режим: Расчет по LTV</b>\n\n"
            f"Стоимость залога: {format_currency(collateral_value)}\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Введите <b>Loan-to-Value (LTV)</b> в %\n"
            "(например: 50 для 50%)"
        )
        await state.set_state(Calc.ltv)
    else:
        await cb.message.edit_text(
            f"<b>💵 Режим: Расчет по сумме займа</b>\n\n"
            f"Стоимость залога: {format_currency(collateral_value)}\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Введите <b>сумму займа</b> в USD\n"
            "(например: 10000)"
        )
        await state.set_state(Calc.borrow)


@dp.message(Calc.ltv)
async def process_ltv(msg: types.Message, state: FSMContext):
    """Обработка LTV"""
    valid, value, error = validate_number(msg.text, min_val=0, max_val=100)
    
    if not valid:
        await msg.answer(
            f"❌ <b>Ошибка:</b> {error}\n\n"
            "LTV должен быть от 0 до 100%\n"
            "Введите LTV:"
        )
        return
    
    await state.update_data(ltv=value / 100)
    data = await state.get_data()
    
    supply_amount = data.get('supply_amount', 0)
    supply_price = data.get('supply_price', 0)
    collateral_value = supply_amount * supply_price
    borrow_amount = collateral_value * (value / 100)
    
    await msg.answer(
        f"✅ <b>LTV: {value}%</b>\n"
        f"Расчетная сумма займа: {format_currency(borrow_amount)}\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Введите <b>Liquidation Threshold (LT)</b> в %\n"
        "(например: 75 для 75%)"
    )
    await state.set_state(Calc.lt)


@dp.message(Calc.borrow)
async def process_borrow(msg: types.Message, state: FSMContext):
    """Обработка суммы займа"""
    valid, value, error = validate_number(msg.text, min_val=0)
    
    if not valid:
        await msg.answer(
            f"❌ <b>Ошибка:</b> {error}\n\n"
            "Введите сумму займа в USD:"
        )
        return
    
    data = await state.get_data()
    supply_amount = data.get('supply_amount', 0)
    supply_price = data.get('supply_price', 0)
    collateral_value = supply_amount * supply_price
    
    if value > collateral_value:
        await msg.answer(
            f"❌ <b>Ошибка:</b> Сумма займа ({format_currency(value)}) "
            f"превышает стоимость залога ({format_currency(collateral_value)})\n\n"
            "Введите корректную сумму займа:"
        )
        return
    
    await state.update_data(borrow=value)
    ltv_percent = (value / collateral_value) * 100 if collateral_value > 0 else 0
    
    await msg.answer(
        f"✅ <b>Сумма займа: {format_currency(value)}</b>\n"
        f"Расчетный LTV: {ltv_percent:.1f}%\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Введите <b>Liquidation Threshold (LT)</b> в %\n"
        "(например: 75 для 75%)"
    )
    await state.set_state(Calc.lt)


@dp.message(Calc.lt)
async def process_lt(msg: types.Message, state: FSMContext):
    """Обработка Liquidation Threshold"""
    valid, value, error = validate_number(msg.text, min_val=0, max_val=100)
    
    if not valid:
        await msg.answer(
            f"❌ <b>Ошибка:</b> {error}\n\n"
            "LT должен быть от 0 до 100%\n"
            "Введите LT:"
        )
        return
    
    await state.update_data(lt=value / 100)
    
    await msg.answer(
        f"✅ <b>Liquidation Threshold: {value}%</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Введите <b>Maximum LTV</b> в %\n"
        "(например: 65 для 65%)"
    )
    await state.set_state(Calc.max_ltv)


# =============================================================================
# CALCULATION - Финальный расчет позиции
# =============================================================================

@dp.message(Calc.max_ltv)
async def calculate_position(msg: types.Message, state: FSMContext):
    """Основной расчет позиции"""
    try:
        # Валидация Max LTV
        valid, max_ltv_input, error = validate_number(msg.text, min_val=0, max_val=100)
        if not valid:
            await msg.answer(
                f"❌ <b>Ошибка:</b> {error}\n\n"
                "Введите Maximum LTV:"
            )
            return
        
        max_ltv = max_ltv_input / 100
        data = await state.get_data()
        
        # Проверяем наличие всех данных
        required = ['supply_ticker', 'borrow_ticker', 'supply_amount', 
                   'supply_price', 'lt', 'mode']
        missing = [f for f in required if f not in data]
        
        if missing:
            await msg.answer(
                f"❌ Отсутствуют данные: {', '.join(missing)}\n\n"
                "Пожалуйста, начните заново с /start"
            )
            await state.clear()
            return
        
        # Извлекаем данные
        supply_amt = data['supply_amount']
        price = data['supply_price']
        lt = data['lt']
        mode = data['mode']
        
        collateral = supply_amt * price
        
        # Рассчитываем займ и LTV в зависимости от режима
        if mode == "mode_ltv":
            ltv = data.get('ltv')
            if ltv is None:
                await msg.answer("❌ Отсутствует LTV. Начните заново с /start")
                await state.clear()
                return
            borrow = collateral * ltv
        else:  # mode_borrow
            borrow = data.get('borrow')
            if borrow is None:
                await msg.answer("❌ Отсутствует сумма займа. Начните заново с /start")
                await state.clear()
                return
            ltv = borrow / collateral if collateral > 0 else 0
        
        ltv_percent = ltv * 100
        
        # Валидация параметров
        if ltv > max_ltv:
            await msg.answer(
                f"❌ <b>Ошибка валидации</b>\n\n"
                f"Current LTV ({ltv_percent:.1f}%) превышает "
                f"Maximum LTV ({max_ltv_input}%)\n\n"
                "Пожалуйста, скорректируйте параметры или начните заново"
            )
            return
        
        if lt <= ltv:
            await msg.answer(
                f"❌ <b>Ошибка валидации</b>\n\n"
                f"Liquidation Threshold ({lt*100:.1f}%) должен быть больше "
                f"Current LTV ({ltv_percent:.1f}%)\n\n"
                "Пожалуйста, скорректируйте параметры или начните заново"
            )
            return
        
        # Основные расчеты
        hf = calculate_health_factor(collateral, lt, borrow)
        liq_price = calculate_liquidation_price(borrow, supply_amt, lt)
        max_borrow = collateral * max_ltv
        buffer = ((price - liq_price) / price) * 100 if price > 0 else 0
        
        # Сценарии падения цены
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
            'max_ltv_percent': max_ltv_input,
            'lt': lt,
            'hf': hf,
            'liq_price': liq_price,
            'buffer': buffer,
            'max_borrow': max_borrow,
            'scenarios': scenarios
        }
        
        # Формируем и отправляем результат
        result_message = build_result_message(data, calculations)
        
        await msg.answer("⏳ Формирую результаты...")
        await msg.answer(result_message)
        
        # Финальное сообщение
        await msg.answer(
            "━━━━━━━━━━━━━━━━━━━━\n"
            "✅ <b>Расчет завершен!</b>\n\n"
            "📝 Для нового расчета: /start\n"
            "ℹ️ Справка: /help\n"
            "📊 Статистика API: /stats"
        )
        
        await state.clear()
        
    except ZeroDivisionError:
        await msg.answer(
            "❌ <b>Ошибка расчета:</b> Деление на ноль\n\n"
            "Проверьте введенные данные\n"
            "Используйте /start для нового расчета"
        )
        await state.clear()
    except Exception as e:
        await msg.answer(
            f"❌ <b>Неожиданная ошибка:</b>\n{str(e)}\n\n"
            "Пожалуйста, начните заново с /start"
        )
        await state.clear()


# =============================================================================
# FALLBACK HANDLER
# =============================================================================

@dp.message()
async def fallback_handler(msg: types.Message, state: FSMContext):
    """Обработчик неизвестных команд"""
    current_state = await state.get_state()
    
    if current_state:
        await msg.answer(
            "⚠️ Пожалуйста, следуйте инструкциям выше\n\n"
            "Команды:\n"
            "• /reset - сбросить текущий расчет\n"
            "• /help - справка"
        )
    else:
        await msg.answer(
            "👋 <b>Привет!</b>\n\n"
            "Я помогу рассчитать параметры вашей DeFi позиции\n\n"
            "Команды:\n"
            "• /start - начать расчет\n"
            "• /help - справка\n"
            "• /supported - список монет с авто-ценами"
        )


# =============================================================================
# ERROR HANDLER
# =============================================================================

@dp.error()
async def error_handler(event, exception):
    """Глобальный обработчик ошибок"""
    print(f"❌ Глобальная ошибка: {exception}")
    import traceback
    traceback.print_exc()
    return True


# =============================================================================
# STARTUP & SHUTDOWN
# =============================================================================

async def on_startup():
    """Действия при запуске бота"""
    print("\n" + "=" * 70)
    print("🚀 DeFi Position Calculator Bot - Starting...")
    print("=" * 70)
    
    # Проверка подключения к Telegram
    try:
        bot_info = await bot.get_me()
        print(f"✅ Бот подключен: @{bot_info.username}")
        print(f"   ID: {bot_info.id}")
        print(f"   Имя: {bot_info.first_name}")
    except Exception as e:
        print(f"❌ ОШИБКА подключения к Telegram: {e}")
        raise
    
    # Проверка CoinGecko API
    try:
        test_price = await price_fetcher.get_price_usd("BTC")
        if test_price:
            print(f"✅ CoinGecko API работает (BTC: ${test_price:,.2f})")
            supported_count = len(price_fetcher.get_supported_symbols())
            print(f"✅ Доступно автоцен: {supported_count} монет")
        else:
            print("⚠️ CoinGecko API может быть недоступен")
    except Exception as e:
        print(f"⚠️ Не удалось проверить CoinGecko: {e}")
    
    print("✅ Ручной ввод цен: доступен для любых токенов")
    print("=" * 70)
    print("✅ БОТ ГОТОВ К РАБОТЕ")
    print("=" * 70)
    print("\n💡 Логи команд будут отображаться здесь...\n")


async def on_shutdown():
    """Действия при остановке бота"""
    print("\n" + "=" * 70)
    print("🛑 Остановка бота...")
    print("=" * 70)
    
    # Закрываем price fetcher
    await price_fetcher.close()
    print("✅ Price fetcher закрыт")
    
    # Закрываем сессию бота
    await bot.session.close()
    print("✅ Бот-сессия закрыта")
    
    print("=" * 70)
    print("👋 Бот успешно остановлен")
    print("=" * 70)


# =============================================================================
# MAIN
# =============================================================================

async def main():
    """Основная функция запуска"""
    try:
        await on_startup()
        
        # Запуск polling
        await dp.start_polling(
            bot, 
            allowed_updates=dp.resolve_used_update_types()
        )
        
    except KeyboardInterrupt:
        print("\n⚠️ Получен сигнал остановки (Ctrl+C)")
    except Exception as e:
        print(f"\n❌ КРИТИЧЕСКАЯ ОШИБКА: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await on_shutdown()


if __name__ == "__main__":
    """Точка входа"""
    try:
        print("\n🔄 Инициализация бота...")
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n👋 Программа завершена пользователем")
    except Exception as e:
        print(f"\n\n❌ ФАТАЛЬНАЯ ОШИБКА: {e}")
        import traceback
        traceback.print_exc()
        exit(1)
