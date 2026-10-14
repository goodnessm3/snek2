import asyncio
import discord
from discord.ext import commands
import json
import random
import sys
import re
import openai

prefixes = ["?", "Snek ", "snek ", "SNEK "]   # note trailing space in name prefix
intents = discord.Intents.default()  # new for version 2.0
intents.message_content = True

TESTING = False

if len(sys.argv) > 1 and sys.argv[1] == "-t":
    TESTING = True
    with open("secret/settings_testing.json", "rb") as f:
        settings = json.load(f)
    bot = commands.Bot(command_prefix=["!"], intents=intents)  # testing bot only responds to !-prefixed cmds

else:
    with open("secret/settings.json", "rb") as f:
        settings = json.load(f)
    bot = commands.Bot(command_prefix=prefixes, intents=intents)

with open("old_commands.txt", "r") as f:
    s = f.read().replace("\n","|^")
    old_command_finder = re.compile("^" + s[:-2])

bot.settings = settings

def is_owner(ctx):
    return ctx.message.author.id == bot.settings["owner_id"]

@bot.event
async def on_ready():

    print("Logged in as {0.user}".format(bot))
    # load default cogs

    with open("default_cogs.json", "rb") as f:
        cogs = json.load(f)
        for c in cogs:
            try:
                await bot.load_extension(c)
                print(f"loaded {c} cog")
            except Exception as e:
                print(f"Error loading {c} cog! See below:")
                (print(e))

    print("loaded default cogs")

@bot.command()
async def test_cmd(ctx):

    await ctx.message.channel.send("Test function")

@bot.command()
async def logout(ctx):

    """increments the accumulator"""

    if ctx.message.author.id == bot.settings["owner_id"]:
        print("logging out")

        bot.cogs["ConvoTracker"].logger.close()  # gracefully close log files

        await ctx.message.channel.send("Bye bye")
        await bot.close()

    else:
        await ctx.message.channel.send("No, YOU logout.")

@bot.command()
async def choose(ctx, *rest):

    """chooses a thing"""

    choices = " ".join(rest).split(" or ")
    await ctx.message.channel.send(random.choice(choices))

@bot.command()
@commands.check(is_owner)
async def reload(ctx, extension):

    if extension == "llm":  # hard coded, sue me
        bot.cogs["ConvoTracker"].logger.close()  # need to nicely close file handles before reloading
        bot.cogs["ConvoTracker"].cancel_tasks()
    try:
        await bot.unload_extension(extension)
        await bot.load_extension(extension)
        await ctx.message.channel.send('''Reloaded {}'''.format(extension))
    except Exception as e:
        await ctx.message.channel.send(str(e))

@bot.command()
@commands.check(is_owner)
async def temp(ctx, t):

    await bot.cogs["ConvoTracker"]._temp(ctx, t)
    # cannot for the life of me work out how to do commands.check inside a cog

@bot.command()
@commands.check(is_owner)
async def do_dream(ctx):

    await bot.cogs["ConvoTracker"].dream()


@bot.event
async def on_message(message):

    if message.author == bot.user:
        return

    #cont = message.content
    ctx = await bot.get_context(message)

    if type(ctx.message.channel) == discord.channel.DMChannel:  # forbid 1:1 DMs
        await ctx.message.channel.send("#robot-zone please")
        return

    #if message.channel.id in bot.cbchannels:
    # commented that out because we can talk to snek anywhere
    if ctx.command is None and message.content.startswith(tuple(prefixes)):
        response = None
        try:

            # the message is addressed to snek but doesn't contain a command, send the text to the LLM for a response
            if bazzer := old_command_finder.match(message.content[5:]):  # user is asking for a command from the old version
                async with message.channel.typing():
                    print("someone asked for an old function")
                    fname = bazzer.group()
                    response = await bot.cogs["ConvoTracker"].forgotten_function(message.author.id, fname)

            else:
                async with message.channel.typing():
                    response = await bot.cogs["ConvoTracker"].get_moderated_response(message.author.id,
                                                                        message.channel.id,
                                                                        message.content[5:])  # strip off "snek "

        except openai.APIConnectionError:
            await message.channel.send("🐍💤")
            bot.cogs["ConvoTracker"].reset_semaphores()

        # SEMAPHORES IF TIMEOUT

        if not response:
            return  # response might just be nothing e.g. if user is spamming
        s = 1900  # max message length
        r = response[:s]

        while r:  # if we get a really long response, need to break it up
            #print(r)
            await message.channel.send(r)
            r = response[s:s+1900]
            s += 1900
            await(asyncio.sleep(1))

        return  # don't also try to process it as a command

    if random.randint(0, 150) == 13:
        await message.add_reaction(chr(127814))  # eggplant

    await bot.process_commands(message)


print(discord.__version__)
bot.run(bot.settings["client_secret"])

#if updating:  # this code runs when the bot loop exits
#    os.execl(sys.executable, "python3", __file__)