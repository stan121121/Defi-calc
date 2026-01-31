import asyncio
import os
import sys
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.client.default import DefaultBotProperties

# ---------- TOKEN SETUP ----------
TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")

if not TOKEN:
    print("❌ Не установлен токен бота. Установите переменную окружения BOT_TOKEN.")
    print("Пример: export BOT_TOKEN='ваш_токен' или создайте файл .env")
    sys.exit(1)

# ---------- BOT INITIALIZATION ----------
bot = Bot(
    token=TOKEN,
    default=DefaultBotProperties(parse_mode="HTML")
)

dp = Dispatcher(storage=MemoryStorage())

# ---------- STATES ----------
class Calc(StatesGroup):
    supply_ticker = State()
    borrow_ticker = State()
    supply_amount = State()
    supply_price = State()
    mode = State()
    ltv = State()
    borrow = State()
    lt = State()
    max_ltv = State()

# ---------- KEYBOARD ----------
mode_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🔢 По LTV", callback_data="mode_ltv")],
    [InlineKeyboardButton(text="💵 По сумме займа", callback_data="mode_borrow")]
])

# ---------- VALIDATION HELPERS ----------
def validate_number(text: str, min_val: float = 0, max_val: float = None) -> tuple[bool, float, str]:
    """Проверяет корректность числового ввода"""
    try:
        text = text.replace(",", ".").strip().replace(" ", "")
        value = float(text)
        
        if value < min_val:
            return False, 0, f"Значение должно быть не меньше {min_val}"
        if max_val is not None and value > max_val:
            return False, 0, f"Значение должно быть не больше {max_val}"
        
        return True, value, ""
    except (ValueError, TypeError):
        return False, 0, "Пожалуйста, введите корректное число"

# ---------- COMMANDS ----------
@dp.message(Command("start"))
async def start_cmd(msg: types.Message, state: FSMContext):
    """Начало работы с ботом"""
    await state.clear()
    await msg.answer(
        "<b>Калькулятор позиции DeFi</b>\n\n"
        "Введите тикер залогового актива (например: ETH, SOL, BTC):"
    )
    await state.set_state(Calc.supply_ticker)

@dp.message(Command("reset"))
async def reset_cmd(msg: types.Message, state: FSMContext):
    """Сброс текущего расчета"""
    await state.clear()
    await msg.answer("✅ Состояние сброшено. Используйте /start для начала расчета.")

@dp.message(Command("help"))
async def help_cmd(msg: types.Message):
    """Помощь по использованию бота"""
    await msg.answer(
        "<b>Помощь по боту:</b>\n\n"
        "<b>Основные команды:</b>\n"
        "/start - начать расчет позиции\n"
        "/reset - сбросить текущий расчет\n"
        "/help - показать это сообщение\n\n"
        "<b>Как использовать:</b>\n"
        "1. Укажите залоговый актив\n"
        "2. Укажите заимствуемый актив\n"
        "3. Введите количество и цену залога\n"
        "4. Выберите режим расчета\n"
        "5. Получите детальный анализ позиции"
    )

# ---------- FLOW HANDLERS ----------
@dp.message(Calc.supply_ticker)
async def process_supply_ticker(msg: types.Message, state: FSMContext):
    """Обработка тикера залогового актива"""
    ticker = msg.text.upper().strip()[:10]
    await state.update_data(supply_ticker=ticker)
    await msg.answer(
        f"Залоговый актив: <b>{ticker}</b>\n\n"
        "Введите тикер заимствуемого актива (например: USDC, DAI):"
    )
    await state.set_state(Calc.borrow_ticker)

@dp.message(Calc.borrow_ticker)
async def process_borrow_ticker(msg: types.Message, state: FSMContext):
    """Обработка тикера заимствуемого актива"""
    ticker = msg.text.upper().strip()[:10]
    await state.update_data(borrow_ticker=ticker)
    await msg.answer(
        f"Заимствуемый актив: <b>{ticker}</b>\n\n"
        "Введите количество залогового актива (например: 1.5):"
    )
    await state.set_state(Calc.supply_amount)

@dp.message(Calc.supply_amount)
async def process_supply_amount(msg: types.Message, state: FSMContext):
    """Обработка количества залогового актива"""
    valid, value, error = validate_number(msg.text, min_val=0.000001)
    
    if not valid:
        await msg.answer(f"❌ {error}\n\nПожалуйста, введите количество:")
        return
    
    await state.update_data(supply_amount=value)
    await msg.answer(
        f"Количество: <b>{value:.6f}</b>\n\n"
        "Введите цену залогового актива в USD (например: 3000):"
    )
    await state.set_state(Calc.supply_price)

@dp.message(Calc.supply_price)
async def process_supply_price(msg: types.Message, state: FSMContext):
    """Обработка цены залогового актива"""
    valid, value, error = validate_number(msg.text, min_val=0.000001)
    
    if not valid:
        await msg.answer(f"❌ {error}\n\nПожалуйста, введите цену:")
        return
    
    await state.update_data(supply_price=value)
    data = await state.get_data()
    
    supply_amount = data.get('supply_amount', 0)
    collateral_value = supply_amount * value
    
    await msg.answer(
        f"<b>📊 Предварительный расчет:</b>\n\n"
        f"Залоговый актив: {data.get('supply_ticker')}\n"
        f"Количество: {supply_amount:.6f}\n"
        f"Цена: ${value:.2f}\n"
        f"<b>Стоимость залога: ${collateral_value:.2f}</b>\n\n"
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
            f"<b>Режим: Расчет по LTV</b>\n\n"
            f"Стоимость залога: ${collateral_value:.2f}\n\n"
            "Введите Loan-to-Value (LTV) в % (например: 50):"
        )
        await state.set_state(Calc.ltv)
    else:
        await cb.message.edit_text(
            f"<b>Режим: Расчет по сумме займа</b>\n\n"
            f"Стоимость залога: ${collateral_value:.2f}\n\n"
            f"Доступно для займа: ${collateral_value:.2f}\n\n"
            "Введите сумму займа:"
        )
        await state.set_state(Calc.borrow)

@dp.message(Calc.ltv)
async def process_ltv(msg: types.Message, state: FSMContext):
    """Обработка LTV"""
    valid, value, error = validate_number(msg.text, min_val=0.01, max_val=99.99)
    
    if not valid:
        await msg.answer(f"❌ {error}\n\nLTV должен быть от 0.01 до 99.99%.\nВведите LTV (%):")
        return
    
    await state.update_data(ltv=value / 100)
    
    data = await state.get_data()
    supply_amount = data.get('supply_amount', 0)
    supply_price = data.get('supply_price', 0)
    collateral_value = supply_amount * supply_price
    borrow_amount = collateral_value * (value / 100)
    
    await msg.answer(
        f"<b>LTV: {value}%</b>\n"
        f"Сумма займа: ${borrow_amount:.2f}\n\n"
        "Введите Liquidation Threshold (LT) в %:"
    )
    await state.set_state(Calc.lt)

@dp.message(Calc.borrow)
async def process_borrow(msg: types.Message, state: FSMContext):
    """Обработка суммы займа"""
    valid, value, error = validate_number(msg.text, min_val=0.01)
    
    if not valid:
        await msg.answer(f"❌ {error}\n\nВведите сумму займа:")
        return
    
    data = await state.get_data()
    supply_amount = data.get('supply_amount', 0)
    supply_price = data.get('supply_price', 0)
    collateral_value = supply_amount * supply_price
    
    if value > collateral_value:
        await msg.answer(
            f"❌ Сумма займа (${value:.2f}) превышает стоимость залога (${collateral_value:.2f})\n\n"
            "Введите корректную сумму:"
        )
        return
    
    await state.update_data(borrow=value)
    
    ltv_percent = (value / collateral_value) * 100 if collateral_value > 0 else 0
    
    await msg.answer(
        f"<b>Сумма займа: ${value:.2f}</b>\n"
        f"LTV: {ltv_percent:.1f}%\n\n"
        "Введите Liquidation Threshold (LT) в %:"
    )
    await state.set_state(Calc.lt)

@dp.message(Calc.lt)
async def process_lt(msg: types.Message, state: FSMContext):
    """Обработка Liquidation Threshold"""
    valid, value, error = validate_number(msg.text, min_val=0.01, max_val=99.99)
    
    if not valid:
        await msg.answer(f"❌ {error}\n\nLT должен быть от 0.01 до 99.99%.\nВведите LT (%):")
        return
    
    await state.update_data(lt=value / 100)
    
    await msg.answer(
        f"<b>Liquidation Threshold: {value}%</b>\n\n"
        "Введите Maximum LTV в %:"
    )
    await state.set_state(Calc.max_ltv)

# ---------- CALCULATION ----------
@dp.message(Calc.max_ltv)
async def calculate_position(msg: types.Message, state: FSMContext):
    """Основной расчет позиции"""
    try:
        # Валидация Max LTV
        valid, max_ltv_input, error = validate_number(msg.text, min_val=0.01, max_val=99.99)
        if not valid:
            await msg.answer(f"❌ {error}\n\nВведите Maximum LTV (%):")
            return
        
        max_ltv = max_ltv_input / 100
        
        # Получаем все данные
        data = await state.get_data()
        
        # Проверяем обязательные поля
        required_fields = ['supply_amount', 'supply_price', 'lt', 'mode']
        missing_fields = [field for field in required_fields if field not in data]
        if missing_fields:
            await msg.answer(
                f"❌ Отсутствуют данные\n\n"
                "Пожалуйста, начните заново с /start"
            )
            await state.clear()
            return
        
        # Извлекаем данные
        supply_amt = data['supply_amount']
        price = data['supply_price']
        lt = data['lt']
        mode = data['mode']
        
        # Рассчитываем стоимость залога
        collateral = supply_amt * price
        
        # Рассчитываем займ и LTV в зависимости от режима
        if mode == "mode_ltv":
            ltv = data.get('ltv', 0)
            borrow = collateral * ltv
            ltv_percent = ltv * 100
        else:
            borrow = data.get('borrow', 0)
            ltv = borrow / collateral if collateral > 0 else 0
            ltv_percent = ltv * 100
        
        # Проверяем валидность данных
        if ltv > max_ltv:
            await msg.answer(
                f"❌ LTV ({ltv_percent:.1f}%) превышает Maximum LTV ({max_ltv_input}%)\n\n"
                "Начните заново с /start"
            )
            await state.clear()
            return
        
        if lt <= ltv:
            await msg.answer(
                f"❌ LT ({lt*100:.1f}%) должен быть больше LTV ({ltv_percent:.1f}%)\n\n"
                "Начните заново с /start"
            )
            await state.clear()
            return
        
        # Основные расчеты
        hf = (collateral * lt) / borrow if borrow > 0 else float('inf')
        liquidation_price = borrow / (supply_amt * lt) if (supply_amt * lt) > 0 else 0
        max_borrow = collateral * max_ltv
        buffer = ((price - liquidation_price) / price) * 100 if price > 0 and liquidation_price > 0 else 0
        
        # Сценарии изменения цены
        price_changes = [-10, -20, -30]
        scenarios = []
        
        for change in price_changes:
            new_price = price * (1 + change/100)
            if borrow > 0:
                new_hf = (supply_amt * new_price * lt) / borrow
            else:
                new_hf = float('inf')
            
            if new_hf <= 1.0:
                emoji = "🔴"
            elif new_hf < 1.3:
                emoji = "🟡"
            else:
                emoji = "🟢"
            
            scenarios.append(f"{change}% → {emoji} HF {new_hf:.2f}")
        
        # Определяем статус позиции
        if hf <= 1.0:
            status = "🔴 ЛИКВИДАЦИЯ"
        elif hf < 1.3:
            status = "🟡 ВНИМАНИЕ"
        elif hf < 2.0:
            status = "🟢 БЕЗОПАСНО"
        else:
            status = "🔵 ОЧЕНЬ БЕЗОПАСНО"
        
        # Формируем ответ
        result_message = (
            f"<b>📊 РАСЧЕТ ПОЗИЦИИ</b>\n\n"
            
            f"<b>Залог:</b>\n"
            f"• Актива: {data.get('supply_ticker', '—')}\n"
            f"• Количество: {supply_amt:.6f}\n"
            f"• Цена: ${price:.2f}\n"
            f"• Стоимость: <b>${collateral:.2f}</b>\n\n"
            
            f"<b>Займ:</b>\n"
            f"• Актива: {data.get('borrow_ticker', '—')}\n"
            f"• Сумма: <b>${borrow:.2f}</b>\n\n"
            
            f"<b>Параметры:</b>\n"
            f"• LTV: <b>{ltv_percent:.2f}%</b>\n"
            f"• Max LTV: {max_ltv_input}%\n"
            f"• LT: {lt*100:.1f}%\n\n"
            
            f"<b>Риски:</b>\n"
            f"• Health Factor: <b>{hf:.2f}</b> ({status})\n"
            f"• Цена ликвидации: <b>${liquidation_price:.2f}</b>\n"
            f"• Буфер: <b>{buffer:.1f}%</b>\n"
            f"• Max займ: ${max_borrow:.2f}\n\n"
            
            f"<b>Сценарии:</b>\n" + "\n".join([f"• {s}" for s in scenarios])
        )
        
        await msg.answer(result_message)
        await msg.answer("Для нового расчета используйте /start")
        
        await state.clear()
        
    except Exception as e:
        await msg.answer(f"❌ Ошибка: {str(e)}\n\nНачните заново с /start")
        await state.clear()

# ---------- FALLBACK HANDLER ----------
@dp.message()
async def fallback_handler(msg: types.Message, state: FSMContext):
    """Обработчик других сообщений"""
    current_state = await state.get_state()
    
    if current_state:
        await msg.answer(
            "⚠️ Следуйте инструкциям выше или используйте /reset для отмены."
        )
    else:
        await msg.answer(
            "Для начала расчета используйте /start\n"
            "Для помощи — /help"
        )

# ---------- MAIN ----------
async def main():
    """Запуск бота"""
    print("=" * 50)
    print("🚀 DeFi Position Calculator Bot")
    print("=" * 50)
    
    try:
        me = await bot.get_me()
        print(f"✅ Бот подключен: @{me.username}")
        print(f"📛 Имя: {me.first_name}")
        print("\n🤖 Бот запущен. Для остановки нажмите Ctrl+C")
        print("=" * 50)
        
        await dp.start_polling(bot)
    except Exception as e:
        print(f"❌ Ошибка запуска: {e}")
    finally:
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Бот остановлен")
