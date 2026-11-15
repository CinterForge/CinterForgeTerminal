#
# HopeDiscord.py
# Copyright 2025 CinterForge, Inc.
#

#pip install discord
import discord
from discord.ext import commands
import socket, urllib
import asyncio
from requests_oauthlib import OAuth2Session



# Initialize Discord Globals
global gHope
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)
machine_name = socket.gethostname()


# TODO
# 1 - Do Proper auth - get key out of here
# 2 - Cinter will not connect to Disocrd until server init and we know she is Cinter
# 3 - Separate Cinter commands and Cinter commands into separate files (like HTTP) - come up with better way to speak to Cinter


from cinter.io.HopeLog import print_log

# Connect and Disconnect calls
def connect_to_discord(hope,user_name, key ):
    print_log("Starting Discord Bot...")
    bot.hope = hope

    hope.discord_bot = bot
    if hope.discord_bot.is_ready():
        return
    
    status_json = "{'status':'CONNECTING'}"
    if hasattr(hope,"httpRequest"):
        hope.httpRequest.send_response(200)
        hope.httpRequest.send_header("Content-type", "text/plain")
        hope.httpRequest.end_headers()
        hope.httpRequest.wfile.write(bytes(status_json, "utf-8"))

    bot.run(key)

async def disconnect_from_discord(hope,user_name) :
    if hasattr(hope,"discord_command") and hope.discord_command:
        await hope.discord_context.send(f'Goodbye!')
    await bot.close()
    if hasattr(hope,"httpRequest"):
        hope.httpRequest.send_response(200)
        hope.httpRequest.send_header("Content-type", "text/plain")
        hope.httpRequest.end_headers()
        hope.httpRequest.wfile.write(bytes(status_json, "utf-8"))

# Get the connection status
async def discord_status(hope,user_name ):
    status_json = "{'status':'UNKNOWN'}"
    if hope.discord_bot.is_ready():
        status_json = "{'status':'CONNECTED'}"
        if hasattr(hope,"discord_command") and hope.discord_command:
            await hope.discord_context.send(f'Connected')
    else:
        status_json = "{'status':'DISCONNECTED'}"
        if hasattr(hope,"discord_command") and hope.discord_command:
            await hope.discord_context.send(f'Not Connected')


    if hasattr(hope,"httpRequest"):
        hope.httpRequest.send_response(200)
        hope.httpRequest.send_header("Content-type", "text/plain")
        hope.httpRequest.end_headers()
        hope.httpRequest.wfile.write(bytes(status_json, "utf-8"))
    return False

# We are connected
@bot.event
async def on_ready():
    global gHope
    print_log(f'{bot.user} has connected to Discord!')
    bot.hope.hopeGeneral = None

    # If this is Cinter, announce your presence
    if bot.hope.is_hope == True:
        for guild in bot.guilds:  # Iterate through each guild (server) the bot is in
            for channel in guild.text_channels:  # Iterate through each text channel in the guild
                if bot.hope.is_hope and channel.name == "hope-comms" and guild.name == "HopeAI's server":
                    bot.hope.hopeGeneral = channel
                    await bot.hope.hopeGeneral.send(f"Hello! I am an instance of Cinter named {machine_name}.  My local http server is running on http://{machine_name}:20380. Type !help to see my commands.")


# Received a command - call proper dispatcher
def isDiscordCommand(name):
    print_log("isDiscordCommand" + name)
    if "channels" in name: return True
    if "hello" in name: return True

###########################
# Discord-specific commands
###########################

# What channels do you see?
async def channels(hope):
    bot = hope.discord_bot
    hopeInitializetionString = f"I am logged into Discord as {bot.user}.  I am a Discord bot that can see the following Discord channels:\n"
    for guild in bot.guilds:  # Iterate through each guild (server) the bot is in
        for channel in guild.text_channels:  # Iterate through each text channel in the guild
            channel_string = f"  Channel: {channel.name} (ID: {channel.id}) in Guild: {guild.name}"
            hopeInitializetionString = hopeInitializetionString + channel_string + "\n"
            # await hope.discord_context.send(channel_string)

    # await hope.discord_context.send(hopeInitializetionString)
    
    # You can also list voice channels, categories, etc.
    hopeInitializetionString = ""
    print_log("\nListing all voice channels:")

    for guild in bot.guilds:
        for channel in guild.voice_channels:
            guild_string = f"  Voice Channel: {channel.name} (ID: {channel.id}) in Guild: {guild.name}"
            hopeInitializetionString = hopeInitializetionString + guild_string + "\n"
            # await hope.discord_context.send(guild_string)

    # await hope.discord_context.send(hopeInitializetionString)

    hopeInitializetionString = ""
    print_log("\nListing all categories:")
    for guild in bot.guilds:
        for category in guild.categories:
            category_string = f"  Category: {category.name} (ID: {category.id}) in Guild: {guild.name}"
            hopeInitializetionString = hopeInitializetionString + category_string + "\n"
            # await hope.discord_context.send(category_string)
    
    # await hope.discord_context.send(hopeInitializetionString)

# All bots report in
async def hello(ctx):
    await ctx.send(f'Hello {ctx.author.display_name}! I am an instance of Cinter named {machine_name}!  My local http server is running on http://{machine_name}:20380')


######################################################
# Cinter's entry point - the name of the machine she is running on
# Working = prompt, psd_json think, title, summarize
# Missing = steamid, image,
######################################################
@bot.command(name = machine_name )
async def hopeai(ctx,*args):
    bot.hope.discord_bot = bot
    bot.hope.discord_context = ctx
    bot.hope.discord_command = True
    
    command_name = args[0]
    arguments = ', '.join(args)
    arguments = arguments.replace(command_name, machine_name)
    arguments = command_name + ", " + arguments
    print_log(arguments)
    try:
        import cintermsg
        cintermsg.send("cinter_discord", {"source": machine_name, "text": arguments})
    except ImportError:
        pass
    bot.hope.commandString = command_name + "?" + arguments

    if isDiscordCommand(arguments):
        if "channels" in arguments:
            await channels(bot.hope)
        elif "hello" in arguments:
            await hello(bot.hope.discord_context)
    else:
       await bot.hope.HandleHopeCommand(bot.hope)


######################################################
# Cinter's current entry point
######################################################
@bot.command(name='cinter')
async def cinter(ctx,*args):
    bot.hope.discord_bot = bot
    bot.hope.discord_context = ctx
    bot.hope.discord_command = True
    arguments = ', '.join(args)
    print_log(arguments)
    try:
        import cintermsg
        cintermsg.send("cinter_discord", {"source": machine_name, "text": arguments})
    except ImportError:
        pass
    arguments1 = "think, " + machine_name + ", " + urllib.parse.quote(arguments)
    print_log(arguments1)
    bot.hope.commandString = "think?" + arguments1
    
    if isDiscordCommand(arguments):
        if "channels" in arguments:
            await channels(bot.hope)
        elif "hello" in arguments:
            await ctx.send(f'Hello {ctx.author.display_name}! I am an instance of Cinter running on {machine_name}! How can I help you?')
    else:
        await bot.hope.HandleHopeCommand(bot.hope)

