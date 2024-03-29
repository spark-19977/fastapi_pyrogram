import asyncio
import logging
import os
import re
from datetime import datetime, timedelta

from pyrogram import Client, raw, filters
from apscheduler.schedulers.asyncio import AsyncIOScheduler as Scheduler
from pyrogram.errors import PhoneNumberInvalid
from sqlalchemy import select, update

from settings import settings
from src.redis import redis, RedisKeys
from .db.models import Base, Keyword, Chat
from .filters import chats_filter
from .scheduler_task import answer_in

class StopError(Exception): pass

class Application:
    def __init__(self):
        self.client = Client(name='main', api_id=settings.api_id, api_hash=settings.api_hash)
        self.scheduler = Scheduler()
        self.scheduler.start()

    async def initialize_pyrogram(self):
        if not self.client.is_connected:
            await self.client.connect()

        await self.on_message_handler()
        await self.client.invoke(raw.functions.updates.GetState())
        await self.client.initialize()
        await self.client.send_message('me', 'started')

        await redis.set(RedisKeys.ready_to_connect, 1)
        await redis.set(RedisKeys.authed, 1)

    async def on_message_handler(self):
        @self.client.on_message(chats_filter & filters.incoming)
        async def read_message(client, message):
            logging.info('callback receive')
            async with Base.session() as session:
                keywords = await session.scalars(select(Keyword).filter_by(chat_id=message.chat.id))

            for keyword in keywords:
                try:
                    text = message.text
                    try:
                        text = message.text.lower()
                    except Exception:
                        pass
                    new_keywords = keyword.keyword.split(',')
                    for _keyword in new_keywords:
                        trans_table = {'.': '\\.', '^': '\\^', '$': '\\$', '*': '\\*', '+': '\\+', '?': '\\?',
                                       '{': '\\{', '}': '\\}', '[': '\\[',
                                       ']': '\\]', '\\': '\\\\', '|': '\\|', '(': '\\(', ')': '\\)'}
                        _keyword = _keyword.translate(_keyword.maketrans(trans_table))
                        if _keyword:
                            if _keyword[0].isalnum():
                                _keyword = r'\b' + _keyword
                            if _keyword[-1].isalnum():
                                _keyword += r'\b'

                        if re.search(_keyword, text):
                            '''delete in future'''
                            async with Base.session() as session:
                                is_active = await session.scalar(select(Chat.is_active).filter_by(id=keyword.chat.id))
                                if keyword.chat.one_time_answer:
                                    await session.execute(
                                        update(Chat).filter_by(id=message.chat.id).values(is_active=False))
                                    await session.commit()
                            if is_active:

                                if keyword.answer_in_seconds < 1:
                                    await client.send_message(chat_id=message.chat.id, text=keyword.answer, )
                                else:
                                    self.scheduler.add_job(answer_in, trigger='date',
                                                           run_date=datetime.now() + timedelta(seconds=keyword.answer_in_seconds),
                                                           kwargs=dict(client=self.client, answer=keyword.answer,
                                                                       chat_id=message.chat.id, mess_id=message.id))
                                # if keyword.chat.one_time_answer:
                                #     async with Base.session() as session:
                                #         await session.execute(update(Chat).filter_by(id=message.chat.id).values(is_active=False))
                                #         await session.commit()
                                raise StopError
                except StopError:
                    break
                except Exception:
                    pass
            logging.info('callback processed')

    async def start_loop(self):
        while True:
            if await redis.get(RedisKeys.send_key):
                logging.info('receive send code signal')
                if not self.client.is_connected:
                    await self.client.connect()
                phone = await redis.get(RedisKeys.phone)
                try:
                    code_hash = await self.client.send_code(phone.decode())
                    await redis.set(RedisKeys.code_hash, code_hash.phone_code_hash)
                except PhoneNumberInvalid:
                    pass
                await redis.delete(RedisKeys.send_key)
            elif code := (await redis.get(RedisKeys.sended_code)):
                try:
                    logging.info('receive sign in signal')
                    code = code.decode()
                    code_hash = await redis.get(RedisKeys.code_hash)
                    code_hash = code_hash.decode()
                    phone = await redis.get(RedisKeys.phone)
                    await self.client.sign_in(phone.decode(), code_hash, code)

                    await self.on_message_handler()
                    await self.initialize_pyrogram()
                finally:
                    await redis.delete(RedisKeys.sended_code)
            elif await redis.get(RedisKeys.logout):
                logging.info('receive logout signal')
                try:
                    await self.client.stop()
                except Exception:
                    ...
                if os.path.exists('main.session'):
                    os.remove('main.session')
                await redis.delete(RedisKeys.authed)
                await redis.delete(RedisKeys.logout)

            await asyncio.sleep(2)
