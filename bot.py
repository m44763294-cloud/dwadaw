import asyncio
import logging
import os
import secrets
import time
from decimal import Decimal, InvalidOperation
from html import escape
from urllib.parse import quote

import aiohttp
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
router = Router()
API = 'https://portal-market.com/partners'
tokens = {}
cards = {}
locks = {}
unlocked_users = set()


class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, 'from_user', None)
        if user is None or user.id not in unlocked_users:
            return
        return await handler(event, data)


async def api(token, method, path, **kwargs):
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.request(method, API + path, headers={'Authorization': 'partners ' + token}, **kwargs) as response:
            if response.status == 204:
                return None
            try:
                payload = await response.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                payload = None
            if response.status >= 400:
                code = payload.get('code', '') if isinstance(payload, dict) else ''
                raise RuntimeError(f'Portals HTTP {response.status} {code}')
            return payload


@router.message(Command('start'), F.chat.type == 'private')
async def start(message: Message):
    await message.answer('Бот показывает входящие офферы Portals. Продажа произойдёт только после подтверждения.\n\n'
        '/token ВАШ_КЛЮЧ — подключить Partner API\n/offers — показать офферы\n/forget — удалить ключ из памяти\n\n'
        'Ключ даёт права на сделки и вывод. Отправляйте его только своему боту; он хранится в памяти до перезапуска.')


@router.message(Command('portaisgobot'), F.chat.type == 'private')
async def unlock(message: Message):
    unlocked_users.add(message.from_user.id)
    await start(message)


@router.message(Command('token'), F.chat.type == 'private')
async def token_handler(message: Message):
    token = (message.text or '').partition(' ')[2].strip()
    if not token or len(token) > 4096 or any(c.isspace() for c in token):
        await message.answer('Формат: /token ВАШ_PARTNER_API_КЛЮЧ')
        return
    try:
        await message.delete()
    except Exception:
        await message.answer('Не удалось удалить сообщение с ключом. Удалите его вручную.')
    tokens[message.from_user.id] = token
    for key in [k for k, v in cards.items() if v['user'] == message.from_user.id]:
        del cards[key]
    await message.answer('Ключ сохранён в памяти. Нажмите /offers для проверки. После перезапуска его нужно ввести снова.')


@router.message(Command('forget'), F.chat.type == 'private')
async def forget(message: Message):
    tokens.pop(message.from_user.id, None)
    for key in [k for k, v in cards.items() if v['user'] == message.from_user.id]:
        del cards[key]
    await message.answer('Ключ удалён из памяти бота.')


@router.message(Command('offers'), F.chat.type == 'private')
async def offers(message: Message):
    user = message.from_user.id
    token = tokens.get(user)
    if not token:
        await message.answer('Сначала отправьте /token ВАШ_КЛЮЧ.')
        return
    lock = locks.setdefault(user, asyncio.Lock())
    if lock.locked():
        await message.answer('Запрос уже выполняется.')
        return
    async with lock:
        try:
            data = await api(token, 'GET', '/offers/received', params={'limit': 100, 'offset': 0})
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
            await message.answer(f'Не удалось получить офферы: {escape(str(exc))}', parse_mode='HTML')
            return
        if not isinstance(data, dict) or not isinstance(data.get('top_offers'), list):
            await message.answer('Portals вернул неизвестный формат ответа.')
            return
        count = 0
        for entry in data['top_offers']:
            wrapper = entry.get('offer') if isinstance(entry, dict) else None
            if not isinstance(wrapper, dict):
                continue
            nft, offer = wrapper.get('nft'), wrapper.get('offer')
            if not isinstance(nft, dict) or not isinstance(offer, dict) or offer.get('status') != 'pending':
                continue
            oid, amount = offer.get('id'), offer.get('amount')
            if not isinstance(oid, str) or not oid or not isinstance(amount, str):
                continue
            try:
                if not Decimal(amount).is_finite() or Decimal(amount) <= 0:
                    continue
            except InvalidOperation:
                continue
            nonce = secrets.token_urlsafe(12)
            name = str(nft.get('name') or 'Подарок')
            number = nft.get('external_collection_number')
            title = name + (f' #{number}' if number is not None else '')
            cards[nonce] = {'user': user, 'offer_id': oid, 'amount': amount, 'title': title, 'state': 'ready', 'created': time.monotonic()}
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Принять', callback_data='offer:' + nonce)]])
            await message.answer(f'🎁 {escape(title)}\n💰 Оффер: <b>{escape(amount)} GRAM</b>\n'
                f'Активных офферов: {escape(str(wrapper.get("offers_count", "?")))}', reply_markup=keyboard, parse_mode='HTML')
            count += 1
        if not count:
            await message.answer('Активных офферов не найдено.')
        elif data.get('total_count', count) > count:
            await message.answer('Показаны первые 100 подарков с офферами.')


@router.callback_query(F.data.startswith('offer:'))
async def choose(callback: CallbackQuery):
    nonce = callback.data.split(':', 1)[1]
    card = cards.get(nonce)
    if not card or card['user'] != callback.from_user.id or card['state'] != 'ready' or time.monotonic() - card['created'] > 900:
        await callback.answer('Оффер недоступен. Обновите /offers.', show_alert=True)
        return
    await callback.answer()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='Подтвердить продажу', callback_data='confirm:' + nonce)],
        [InlineKeyboardButton(text='Отмена', callback_data='cancel:' + nonce)],
    ])
    await callback.message.answer(f'Подтвердить продажу <b>{escape(card["title"])}</b> за '
        f'<b>{escape(card["amount"])} GRAM</b>? Portals удержит комиссию. '
        'После подтверждения подарок перейдёт покупателю.', reply_markup=keyboard, parse_mode='HTML')


@router.callback_query(F.data.startswith('cancel:'))
async def cancel(callback: CallbackQuery):
    nonce = callback.data.split(':', 1)[1]
    card = cards.get(nonce)
    if not card or card['user'] != callback.from_user.id:
        await callback.answer('Недоступно.', show_alert=True)
        return
    if card['state'] != 'ready':
        await callback.answer('Операция уже выполняется или завершена.', show_alert=True)
        return
    card['state'] = 'cancelled'
    await callback.answer('Отменено')
    await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data.startswith('confirm:'))
async def confirm(callback: CallbackQuery):
    nonce = callback.data.split(':', 1)[1]
    card = cards.get(nonce)
    user = callback.from_user.id
    if not card or card['user'] != user or card['state'] != 'ready' or time.monotonic() - card['created'] > 900 or user not in tokens:
        await callback.answer('Оффер недоступен. Обновите /offers.', show_alert=True)
        return
    lock = locks.setdefault(user, asyncio.Lock())
    if lock.locked():
        await callback.answer('Подождите завершения предыдущего запроса.', show_alert=True)
        return
    card['state'] = 'processing'
    await callback.answer()
    async with lock:
        try:
            await api(tokens[user], 'POST', '/offers/' + quote(card['offer_id'], safe='') + '/accept', json={'amount': card['amount']})
        except (aiohttp.ClientError, asyncio.TimeoutError):
            card['state'] = 'unknown'
            await callback.message.answer('Ответ не получен: продажа могла пройти. Проверьте Portals перед повторной попыткой.')
        except RuntimeError as exc:
            card['state'] = 'failed'
            await callback.message.answer(f'Portals отклонил запрос: {escape(str(exc))}. Обновите /offers.', parse_mode='HTML')
        else:
            card['state'] = 'accepted'
            await callback.message.answer('Portals подтвердил принятие оффера и продажу подарка.')
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logging.warning('Не удалось убрать кнопки подтверждения')


@router.message(F.chat.type == 'private')
async def help_message(message: Message):
    await message.answer('Привет! 👋 Я показываю входящие офферы Portals.\n\n'
        'Отправьте /start для инструкции или /offers для списка офферов.\n'
        'Ключ подключается командой /token КЛЮЧ; не отправляйте его другим ботам.')


async def main():
    token = os.getenv('BOT_TOKEN', '').strip()
    if not token:
        raise SystemExit('Укажите BOT_TOKEN в .env')
    bot = Bot(token)
    dp = Dispatcher()
    gate = AccessMiddleware()
    # The unlock command must reach its handler before the access check.
    @dp.message.outer_middleware()
    async def message_gate(handler, event, data):
        text = getattr(event, 'text', '') or ''
        command = text.split(maxsplit=1)[0].split('@', 1)[0].lower() if text else ''
        if command == '/portaisgobot' and event.chat.type == 'private':
            return await handler(event, data)
        return await gate(handler, event, data)

    dp.callback_query.outer_middleware(gate)
    dp.include_router(router)
    try:
        me = await bot.get_me()
        print(f'Бот запущен: @{me.username}. Откройте личный чат с ним и отправьте /start.', flush=True)
        webhook = await bot.get_webhook_info()
        if webhook.url:
            print('ВНИМАНИЕ: у этого бота включён webhook. Polling не заработает, пока webhook не отключён. Проверьте другие запущенные приложения.', flush=True)
            return
        await bot.set_my_commands([
            BotCommand(command='start', description='Приветствие и инструкция'),
            BotCommand(command='offers', description='Входящие офферы'),
            BotCommand(command='token', description='Подключить Partner API'),
            BotCommand(command='forget', description='Удалить ключ из памяти'),
        ])
        await dp.start_polling(bot, allowed_updates=['message', 'callback_query'])
    finally:
        await bot.session.close()


if __name__ == '__main__':
    asyncio.run(main())
