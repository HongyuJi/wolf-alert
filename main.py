import time

from binance.lib.utils import config_logging
from dotenv import load_dotenv
from discord.ext import commands, tasks
import os
from binance.spot import Spot as Client
import discord
import logging
from enum import Enum
from apscheduler.schedulers.background import BackgroundScheduler


class PriceStatus(Enum):
    PriceStart = 0
    PriceDown = 1
    PriceUp = 2


load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
key = os.getenv('CLIENT_KEY')
secret = os.getenv('CLIENT_SECRET')
config_logging(logging, logging.WARNING)

client = commands.Bot(command_prefix=['!', '+', '-', '?'], intents=discord.Intents.default())
spot_client = Client(key, secret)

global_price_map = {}
scheduler = BackgroundScheduler()

def help_convert_token(token_prefix_lower_case):
    return token_prefix_lower_case.upper() + "USDT"


def find_price(token_prefix_lower_case):
    local_count = 0
    token = help_convert_token(token_prefix_lower_case)
    if token in global_price_map:
        return global_price_map[token]
    print("start price look up on", token_prefix_lower_case)
    scheduler.add_job(price_job(token),'interval',seconds=1)
    while token not in global_price_map and local_count < 5:
        price_job(token)()
        local_count += 1
        time.sleep(1)
    return global_price_map[token]


def price_job(real_token):
    def real_job():
        try:
            _price = spot_client.ticker_price(real_token)
            price = _price['price']
            global_price_map[real_token] = price
            return price
        except Exception as e:
            return None
    return real_job



print(discord.__version__)


def swap_status(status: PriceStatus):
    return PriceStatus.PriceUp if status == PriceStatus.PriceDown else PriceStatus.PriceDown


class AlertTickerInfo:
    def __init__(self, _token: str, _alert_price=0, _status=PriceStatus.PriceStart):
        self.alert_price = _alert_price
        self.token = _token
        self.status = _status

    def __str__(self):
        return str(self.alert_price) + self.token + str(self.status.name)

class AlertUser(object):
    def __init__(self,_username,_token):
        self.username = _username
        self.token = _token

    def __str__(self):
        return self.username + ":" +  self.token

    def __eq__(self, other):
        return self.__str__() == other.__str__()

    def __hash__(self):
        return hash("{}:{}".format(self.username,self.token))


# register the price alert gap token -> AlertTickerInfo (global var)
start_price_map = {}
class PriceAtLoop:
    def __init__(self, ctx, _token, _target_price):
        self.token = _token
        self.target_price = _target_price
        self.task = self.price_alert_at.start(ctx, _token, _target_price)

    async def price_alert(self, ctx, line_info: AlertTickerInfo, now_price):
        line_status = line_info.status
        logging.debug(f'line_info={line_info},now_price={now_price}')
        # init state update
        if line_status == PriceStatus.PriceStart:
            line_info.status = PriceStatus.PriceUp if line_info.alert_price > now_price else PriceStatus.PriceDown
            start_price_map[AlertUser(ctx.author.name,line_info.token)] = line_info
            return

        # check condition matches,then send alert
        if line_status is PriceStatus.PriceUp and now_price > line_info.alert_price or \
                line_status is PriceStatus.PriceDown and now_price < line_info.alert_price:
            line_info.status = swap_status(line_status)
            await ctx.author.send(f",{line_info.token} now price = {now_price}", ephemeral=True)

        start_price_map[AlertUser(ctx.author.name,line_info.token)] = line_info
        return

    @tasks.loop(seconds=1)
    async def price_alert_at(self, ctx, content, target_price):
        try:
            _price = find_price(content)
            now_price = float(_price)
            if content not in start_price_map:
                start_price_map[AlertUser(ctx.author.name,content)] = AlertTickerInfo(content, target_price)
            await self.price_alert(ctx, start_price_map[content], now_price)
        except Exception as e:
            logging.error(e)

    def cancel(self):
        self.task.cancel()

    def __str__(self):
        return f'alert price={self.target_price}'


class PriceAtGap:

    def __init__(self, _ctx, _token, init_price, _gap):
        self.token_prefix = _token
        self.now_price = init_price
        self.gap = _gap
        self.ctx = _ctx
        self.task = self.price_alert_at.start()

    @tasks.loop(seconds=1)
    async def price_alert_at(self):
        try:
            now_price = find_price(self.token_prefix)
            if abs(float(now_price) - float(self.now_price)) > float(self.gap):
                self.now_price = now_price
                await self.ctx.author.send(f"<@{self.ctx.author.id}>,{self.token_prefix},now price = {now_price}")
        except Exception as e:
            logging.error(e)

    def cancel(self):
        self.task.cancel()

    def __str__(self):
        return f'alert gap={self.gap},now watermark={self.now_price}'


class RemindCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self._bot = bot
        # register loop token task :token -> PriceAtLoop
        self.loop_task_register = {}
        # register gap token task : token -> PriceAtGap
        self.gap_task_register = {}

    @commands.command("remind", help="add/cancel remind me when price get across,[+remind eth 1500],[-remind eth]")
    async def remind_price(self, ctx, token, price=0.0):
        if ctx.prefix == "+":
            await self.loop_check(ctx, token, price)
        elif ctx.prefix == "-":
            await self.cancel_loop_check(ctx, token)
        InfoCog.review_loop_task(self.loop_task_register)

    async def loop_check(self, ctx, token, price):
        try:
            check_task = PriceAtLoop(ctx, token, price)
            if token in self.loop_task_register:
                logging.warning("token=%s covered by new task", token)
                self.loop_task_register[AlertUser(ctx.author.name,token)].cancel()
            self.loop_task_register[AlertUser(ctx.author.name,token)] = check_task
            await ctx.author.send(f' {token} price alert at {price} start')
        except Exception as e:
            print(e)
            await ctx.author.send(f' remind create fail')

    async def cancel_loop_check(self, ctx, token):
        try:
            task_if = self.loop_task_register[AlertUser(ctx.author.name,token)]
            if not task_if:
                await ctx.author.send(f' no task found')
                return
            task_if.cancel()
            del self.loop_task_register[AlertUser(ctx.author.name,token)]
            await ctx.author.send(f' {token} remind canceled')
        except Exception as e:
            print(e)
            return

    @commands.command("gap", help="check when attr gap reach,[+gap eth 10],[-gap eth]")
    async def gap_check(self, ctx, token, gap=0.0):
        if ctx.prefix == "+":
            await self.setup_gap(ctx, token, gap)
        elif ctx.prefix == "-":
            await self.cancel_gap(ctx, token)
        InfoCog.review_gap_task(self.gap_task_register)

    async def setup_gap(self, ctx, token, gap=0.0):
        try:
            check_user = AlertUser(ctx.author.name,token)
            init_price = find_price(token)
            check_task = PriceAtGap(ctx, token, init_price, gap)
            if check_user in self.gap_task_register:
                logging.warning("token=%s covered by new task", token)
                self.gap_task_register[check_user].cancel()
            self.gap_task_register[check_user] = check_task
            await ctx.author.send(f' {token} price alert with {gap} start! ')
            return
        except Exception as e:
            print(e)
            await ctx.author.send(f' remind create fail')

    async def cancel_gap(self, ctx, token):
        try:
            check_user = AlertUser(ctx.author.name,token)

            if check_user in self.gap_task_register:
                self.gap_task_register[check_user].cancel()
                del self.gap_task_register[check_user]
                await ctx.author.send(f' {token} gap task canceled')
            else:
                await ctx.author.send(f' no task found')
                return

        except Exception as e:
            print(e)
            return


class InfoCog(commands.Cog, name="info"):
    loop_task = {}
    gap_task = {}

    def __init__(self):
        pass

    @staticmethod
    def review_loop_task(_loop_task: dict):
        InfoCog.loop_task = _loop_task

    @commands.command(name="remind?", help="review current running loop task ")
    async def print_loop_task(self, ctx):
        embed = discord.Embed(
            title="Running Remind Task",
            description="this lists all running loop check task list,to add one,use [+remind eth 1550],or remove with [-remind eth]",
        )
        embed_str = ""
        for k, v in InfoCog.loop_task.items():
            if v:
                embed_str += f'token={k},value={v.__str__()}\n'
        if not embed_str:
            embed_str = "sorry, task not found"
        embed.add_field(
            name="loop task",
            value=embed_str
        )
        await ctx.send(embed=embed)

    @staticmethod
    def review_gap_task(_gap_task):
        InfoCog.gap_task = _gap_task

    @commands.command(name="gap?", help="review current gap task")
    async def print_gap_task(self, ctx):
        embed = discord.Embed(
            title="Running Gap Task",
            description="this lists all running loop check task list,to add one,use [+remind eth 1550],or remove with [-gap eth]",
        )
        embed_str = ""
        for k, v in InfoCog.gap_task.items():
            if v:
                embed_str += f'token={k},value={v.__str__()}\n'
        if not embed_str:
            embed_str = "sorry, task not found"
        embed.add_field(
            name="gap task",
            value=embed_str
        )
        await ctx.send(embed=embed)


@client.event
async def on_ready():
    print(f'We have logged in as {client.user}')
    print(f'{client.user} has connected to Discord!')


@client.event
async def on_message(message):
    if message.author == client.user:
        return
    if message.content.startswith('$'):
        try:
            price = find_price(message.content[1:])
            print(price)
            await message.channel.send(price)
        except Exception as e:
            await message.channel.send("sorry,price not found!")
    else:
        await client.process_commands(message)


if __name__ == '__main__':
    client.add_cog(RemindCog(client))
    client.add_cog(InfoCog())
    scheduler.start()
    client.run(TOKEN)
