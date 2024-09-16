import discord
from discord.ext import commands
import asyncio
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import yt_dlp
import os
from discord.errors import HTTPException

intents = discord.Intents.default()
intents.message_content = True

PREFIX = '!'

# Configurações do Spotify
sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(client_id="CLIENT_ID",
                                                           client_secret="CLIENT_SECRET"))

# Configurações do YT-DLP
ytdl_format_options = {
    'format': 'bestaudio/best',
    'extractaudio': True,
    'audioformat': 'mp3',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0'
}

ytdl = yt_dlp.YoutubeDL(ytdl_format_options)

class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('url')

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=False):
        loop = loop or asyncio.get_event_loop()
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=not stream))
        
        if 'entries' in data:
            data = data['entries'][0]

        filename = data['url'] if stream else ytdl.prepare_filename(data)
        return cls(discord.FFmpegPCMAudio(filename, options='-vn'), data=data)

class MusicBot(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.queue = {}
        self.loop = {}
        self.last_activity = {}
        self.inactivity_timer = {}
        self.is_playing = {}

    async def safe_send(self, ctx, content=None, embed=None):
        try:
            if embed:
                await ctx.send(embed=embed)
            else:
                await ctx.send(content)
        except HTTPException as e:
            if e.code == 429:  # Rate limit error
                retry_after = e.retry_after
                print(f"Rate limited. Retrying in {retry_after} seconds.")
                await asyncio.sleep(retry_after)
                await self.safe_send(ctx, content, embed)
            else:
                print(f"HTTP Exception: {e}")

    @commands.command()
    async def play(self, ctx, *, query):
        voice_channel = ctx.author.voice.channel
        if not voice_channel:
            return await self.safe_send(ctx, "Você precisa estar em um canal de voz para usar este comando.")

        if ctx.voice_client is None:
            await voice_channel.connect()
        elif ctx.voice_client.channel != voice_channel:
            await ctx.voice_client.move_to(voice_channel)

        if ctx.guild.id not in self.queue:
            self.queue[ctx.guild.id] = []
        
        if ctx.guild.id not in self.loop:
            self.loop[ctx.guild.id] = False

        if query.startswith('https://open.spotify.com/track/'):
            track_id = query.split('/')[-1].split('?')[0]
            track = sp.track(track_id)
            query = f"{track['name']} {track['artists'][0]['name']}"

        try:
            player = await YTDLSource.from_url(query, loop=self.bot.loop, stream=True)
            self.queue[ctx.guild.id].append(player)
            await self.safe_send(ctx, f"Adicionado à fila: {player.title}")

            if not ctx.voice_client.is_playing():
                await self.play_next(ctx)

            self.update_activity(ctx.guild.id)
        except Exception as e:
            await self.safe_send(ctx, f"Ocorreu um erro ao buscar a música: {str(e)}")

    async def play_next(self, ctx):
        if not self.queue[ctx.guild.id]:
            self.is_playing[ctx.guild.id] = False
            await self.handle_empty_queue(ctx)
            return

        self.is_playing[ctx.guild.id] = True
        player = self.queue[ctx.guild.id][0]

        def after_playing(e):
            if self.loop[ctx.guild.id]:
                self.queue[ctx.guild.id].append(self.queue[ctx.guild.id].pop(0))
            else:
                self.queue[ctx.guild.id].pop(0)
            asyncio.run_coroutine_threadsafe(self.play_next(ctx), self.bot.loop)

        ctx.voice_client.play(player, after=after_playing)
        await self.safe_send(ctx, f"Tocando agora: {player.title}")
        self.update_activity(ctx.guild.id)

    def update_activity(self, guild_id):
        self.last_activity[guild_id] = asyncio.get_event_loop().time()
        if guild_id in self.inactivity_timer:
            self.inactivity_timer[guild_id].cancel()
        self.inactivity_timer[guild_id] = asyncio.create_task(self.check_inactivity(guild_id))

    async def handle_empty_queue(self, ctx):
        embed = discord.Embed(
            title="Fila Vazia",
            description="A fila está vazia. Saindo do canal de voz em 3 minutos se nenhuma música for adicionada.",
            color=discord.Color.orange()
        )
        await self.safe_send(ctx, embed=embed)
        self.update_activity(ctx.guild.id)

    async def check_inactivity(self, guild_id):
        await asyncio.sleep(180)  # 3 minutos
        current_time = asyncio.get_event_loop().time()
        if current_time - self.last_activity[guild_id] >= 180 and not self.is_playing.get(guild_id, False):
            guild = self.bot.get_guild(guild_id)
            if guild:
                voice_client = guild.voice_client
                if voice_client:
                    await voice_client.disconnect()
                    channel = voice_client.channel
                    embed = discord.Embed(
                        title="Desconectado",
                        description="Saí do canal de voz devido à inatividade. Use o comando !play para tocar mais músicas.",
                        color=discord.Color.red()
                    )
                    await channel.send(embed=embed)

    @commands.command()
    async def resume(self, ctx):
        if ctx.voice_client and ctx.voice_client.is_paused():
            ctx.voice_client.resume()
            await self.safe_send(ctx, "Música resumida.")
            self.update_activity(ctx.guild.id)
        else:
            await self.safe_send(ctx, "A música não está pausada.")

    @commands.command()
    async def loop(self, ctx):
        if ctx.guild.id not in self.loop:
            self.loop[ctx.guild.id] = False
        
        self.loop[ctx.guild.id] = not self.loop[ctx.guild.id]
        await self.safe_send(ctx, f"Loop {'ativado' if self.loop[ctx.guild.id] else 'desativado'}.")
        self.update_activity(ctx.guild.id)

    @commands.command()
    async def pause(self, ctx):
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.pause()
            await self.safe_send(ctx, "Música pausada.")
            self.update_activity(ctx.guild.id)
        else:
            await self.safe_send(ctx, "Não há nenhuma música tocando.")

    @commands.command()
    async def leave(self, ctx):
        if ctx.voice_client:
            await ctx.voice_client.disconnect()
            embed = discord.Embed(
                title="Desconectado",
                description="Saí do canal de voz. Use o comando !play para tocar mais músicas.",
                color=discord.Color.red()
            )
            await self.safe_send(ctx, embed=embed)
            if ctx.guild.id in self.is_playing:
                del self.is_playing[ctx.guild.id]
        else:
            await self.safe_send(ctx, "Não estou em nenhum canal de voz.")

    @commands.command()
    async def volume(self, ctx, volume: int):
        if ctx.voice_client is None:
            return await self.safe_send(ctx, "Não estou conectado a um canal de voz.")

        ctx.voice_client.source.volume = volume / 100
        await self.safe_send(ctx, f"Volume alterado para {volume}%")
        self.update_activity(ctx.guild.id)

    @commands.command()
    async def skip(self, ctx):
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.stop()
            await self.safe_send(ctx, "Música pulada.")
            self.update_activity(ctx.guild.id)
        else:
            await self.safe_send(ctx, "Não há música tocando no momento.")

    @commands.command(name='musichelp')
    async def music_help(self, ctx):
        commands_list = [
            f"{PREFIX}play [música ou URL do Spotify] - Toca uma música ou adiciona à fila",
            f"{PREFIX}leave - Sai do canal de voz",
            f"{PREFIX}pause - Pausa a música atual",
            f"{PREFIX}resume - Retoma a música pausada",
            f"{PREFIX}loop - Ativa/desativa o loop da música atual",
            f"{PREFIX}skip - Pula a música atual",
            f"{PREFIX}volume [0-100] - Ajusta o volume da música",
            f"{PREFIX}now_playing - Veja a música que está tocando",
            f"{PREFIX}musichelp - Mostra esta mensagem de ajuda"
        ]
        
        help_text = "\n".join(commands_list)
        embed = discord.Embed(title="Comandos do Bot de Música", description=help_text, color=discord.Color.blue())
        await self.safe_send(ctx, embed=embed)
        self.update_activity(ctx.guild.id)

async def setup(bot):
    await bot.add_cog(MusicBot(bot))

class MyBot(commands.AutoShardedBot):
    async def setup_hook(self):
        await setup(self)
        print("Cog MusicBot adicionado.")

bot = MyBot(command_prefix=PREFIX, intents=intents)

@bot.event
async def on_ready():
    print(f'Bot conectado como {bot.user}')
    print(f'Usando {len(bot.shards)} shards')

bot.run('TOKEN_DO_SEU_BOT')
