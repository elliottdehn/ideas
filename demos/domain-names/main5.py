import asyncio
import aiohttp
import time
from itertools import product
from typing import Set
import logging
import random
from collections import defaultdict
import math
from collections import Counter
from pathlib import Path
import aiodns
from openai import OpenAI

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

client = OpenAI(api_key="")

async def is_speakable(domain) -> bool:
    speech_file_path = Path(__file__).parent / f"sound/{domain}.mp3"
    into = f"Visit {domain} today."

    # Run the blocking creation call in a thread
    response = await asyncio.to_thread(
        client.audio.speech.create,
        model="tts-1",
        voice="alloy",
        input=into
    )
    # Also run streaming the file in a thread
    await asyncio.to_thread(response.stream_to_file, speech_file_path)

    # Opening and transcription calls in a thread as well
    audio_file = await asyncio.to_thread(open, speech_file_path, "rb")
    transcription = await asyncio.to_thread(
        client.audio.transcriptions.create,
        model="whisper-1",
        file=audio_file
    )

    print(transcription.text)
    return transcription.text.lower() == into.lower()

class DomainChecker:
    def __init__(self, max_concurrent: int = 1000, timeout: float = 2.0):
        self.max_concurrent = max_concurrent
        self.timeout = timeout
        self.checked_domains = set()
        self.session = None
        self.start_time = None
        self.domains_processed = 0
        self.available_found = 0
        self.last_stats_time = time.time()
        # Open the file in append mode at initialization
        self.output_file = open("available_domains.txt", "a", buffering=1)  # Line buffering
        self.dns_resolver = aiodns.DNSResolver()  # DNS Resolver instance

    async def initialize(self):
        """Initialize aiohttp session with optimal settings"""
        conn = aiohttp.TCPConnector(
            limit=self.max_concurrent,
            ttl_dns_cache=300,
            use_dns_cache=True,
            ssl=False
        )
        self.session = aiohttp.ClientSession(
            connector=conn,
            timeout=aiohttp.ClientTimeout(total=self.timeout)
        )

    async def has_a_record(self, domain: str) -> bool:
        """
        Check if the domain has an A record via DNS.
        If it does, it's likely not available.
        """
        try:
            # Strip the TLD for the DNS query if needed, or leave as is (e.g. example.com)
            # aiodns expects full domain
            result = await self.dns_resolver.query(domain, 'A')
            if result:
                return True
        except aiodns.error.DNSError:
            # No A record found
            return False
        return False

    async def check_domain_availability(self, domain: str) -> bool:
        """Check domain availability using RDAP only"""
        if domain in self.checked_domains:
            return False

        # 1. DNS Check: If it resolves, consider it unavailable immediately.
        """
        if await self.has_a_record(domain):
            self.domains_processed += 1
            return False
        """

        self.checked_domains.add(domain)
        rdap_url = f"https://rdap.verisign.com/com/v1/domain/{domain}"

        try:
            async with self.session.get(rdap_url) as response:
                self.domains_processed += 1
                if response.status == 404:
                    self.available_found += 1
                    # Write directly to file with immediate flush
                    # if await is_speakable(domain):
                    print(f"!{domain}", file=self.output_file, flush=True)
                    return True
                return False
        except Exception as e:
            self.domains_processed += 1
            return False

    def generate_domain_stream(self, words: Set[str], min_length: int = 3, max_length: int = 15):
      """Generate domains as an iterator, mixing lengths, yielding them in a round-robin fashion by length.
        For example:
          - First yield one domain of length `min_length` (if available),
          - then one domain of length `min_length+1`,
          - ... up to `max_length`,
          - then loop back to `min_length` again, continuing until all are exhausted.
      """
      # Create a set of all combinations, then bucket by length
      ds = {f"{w1}{w2}.com" for w1 in words for w2 in words}

      print(len(ds))

      ds_by_length = defaultdict(list)
      for d in ds:
          ds_by_length[len(d)].append(d)

      # Filter only lengths that actually have domains
      valid_lengths = [l for l in range(min_length, max_length + 1) if ds_by_length[l]]
      print(valid_lengths)

      # Continue round-robin until all lists are empty
      while any(ds_by_length[l] for l in valid_lengths):
          for l in valid_lengths:
              if ds_by_length[l]:
                  # Pop from the end for efficiency; order is not specified as important
                  yield ds_by_length[l].pop()

    async def print_stats(self):
        """Print periodic statistics"""
        current_time = time.time()
        duration = current_time - self.last_stats_time
        total_duration = current_time - self.start_time

        domains_per_sec = self.domains_processed / total_duration if total_duration > 0 else 0

        logger.info(f"Status: {self.domains_processed} checked, {self.available_found} available "
                   f"({domains_per_sec:.1f} domains/sec)")

        self.last_stats_time = current_time

    async def stats_monitor(self):
        """Periodically print statistics"""
        while True:
            await self.print_stats()
            await asyncio.sleep(5)

    async def process_stream(self, domain_stream, chunk_size: int = 500):
        """Process domains from the stream concurrently"""
        chunk = []
        for domain in domain_stream:
            chunk.append(domain)
            if len(chunk) >= chunk_size:
                tasks = [self.check_domain_availability(d) for d in chunk]
                await asyncio.gather(*tasks)
                chunk = []
                await asyncio.sleep(0.1)

        if chunk:
            tasks = [self.check_domain_availability(d) for d in chunk]
            await asyncio.gather(*tasks)

    async def run(self, words: Set[str], chunk_size: int = 500):
        """Main execution flow"""
        self.start_time = time.time()
        self.last_stats_time = self.start_time
        await self.initialize()

        try:
            stats_task = asyncio.create_task(self.stats_monitor())
            domain_stream = self.generate_domain_stream(words, min_length=3, max_length=12)
            await self.process_stream(domain_stream, chunk_size)

        except KeyboardInterrupt:
            logger.info("Shutting down gracefully...")
        finally:
            stats_task.cancel()
            await self.session.close()
            await self.print_stats()
            self.output_file.close()  # Close the file handle

async def main():
    # Your existing words set here
    words = {
        "lob",
        "ob",
        "lo",
        "abacus",
"ablaze",
"abloom",
"abound",
"acumen",
"adage",
"addle",
"affable",
"afield",
"agape",
"aglow",
"magic",
"ajar",
"ala", "crity",
"alch", "emy",
"alga",
"alias",
"al", "ight",
"allure",
"aloof",
"amity",
"amok",
"amply",
"amuse",
"new",
"arbor",
"ardor",
"arise",
"apex",
"attune",
"avail",
"avert",
"bard",
"bask",
"befit",
"be",
"bloom",
"bode",
"boon",
"bounty",
"calico",
"canopy",
"cider",
"cobble",
"codify",
"avenue",
"cohort",
"condor",
"confer",
"trading",
"corky",
"cosy",
"coven",
"covet",
"cranny",
"credo",
"creel",
"croon",
"cubby",
"solar",
"city",
"pal",
"bay",
"curio",
"cusp",
"daggle",
"dally",
"dapper",
"dapple",
"deft",
"deign",
"delve",
"demure",
"denim",
"depot",
"dogma",
"druid",
"ducky",
"edict",
"edify",
"efface",
"elicit",
"elixir",
"elope",
"elude",
"embed",
"emote",
"encore",
"endow",
"enfold",
"engage",
"engulf",
"ennui",
"ensue",
"entice",
"entreat",
"envoy",
"epoxy",
"equip",
"erode",
"erupt",
"espy",
"ester",
"ethos",
"evade",
"evoke",
"exact",
"exalt",
"exude",
"fable",
"fallow",
"fancy",
"fated",
"fauna",
"fealty",
"feign",
"fey",
"fickle",
"firth",
"ficus",
"filly",
"filmy",
"finch",
"fiord",
"flail",
"flair",
"fleck",
"flick",
"flint",
"flit",
"foist",
"folio",
"foray",
"forge",
"forte",
"fray",
"frill",
"frond",
"froth",
"fungal",
"funky",
"fusty",
"futon",
"gable",
"gaily",
"gamely",
"gamut",
"gavel",
"rhino",
"hero",
"lion",
"zebra",
"horse",
"ele",
"giddy",
"gild",
"gizmo",
"goner",
"goose",
"gouge",
"gourd",
"graft",
"grain",
"grand",
"grant",
"grasp",
"gratis",
"greed",
"grist",
"grove",
"grump",
"guava",
"gusto",
"gusty",
"haiku",
"hallow",
"hasty",
"hefty",
"heist",
"helix",
"rage",
"fury",
"chaos",
"chat",
"agent",
"evil",
"nemo",
"yep",
"taken",
"take",
"make",
"order",
"clob",
"lick",
"flick",
"tap",
"root",
"g",
"research",
"lab",
"labs",
"go",
        "meta", "nova", "aqua", "spark", "proto", "cortex", "aim", "high", "ail", "lex", "ex",
        "peak", "ultra", "quant", "ampli", "fort", "ideal", "aero", "ly", "ify", "neo", "near", "intel", "el", "check", "r",
        "io", "able", "uni", "neon", "aura", "via", "ari", "ora", "ava", "muse", "nia", "ia", "eco", "bloom",
        "holo", "prism", "magi", "luna", "vital", "hero", "omni", "zen", "zeta", "ck", "che", "loom", "sha", "shaman",
        "plex", "veri", "aqui", "astro", "vela", "nexo", "synth", "migo", "ito", "oro", "hex", "octa", "pus",
        "base", "hub", "grid", "spot", "space", "craft", "station", "state", "mono", "istic", "mark", "ark", "arc",
        "force", "front", "book", "nation", "bridge", "core", "team", "circle", "boot", "boo", "oo", "o", "oot",
        "market", "trade", "x", "alpha", "beta", "delta", "gamma", "omega", "scream", "ice",
        "tech", "data", "link", "connect", "matrix", "light", "prime", "cloud", "pay", "cheap", "veri", "sign", "ver", "i", "sig", "n",
        "hash", "secure", "sec", "chain", "crypto", "o", "cry", "block", "stream", "flow", "peak",
        "mountain", "river", "forest", "field", "valley", "plain", "ocean", "help", "news", "media", "tain", "move", "sign", "link",
        "sea", "coast", "shore", "harbor", "port", "road", "path", "way", "idea",
        "route", "line", "node", "edge", "vertex", "point", "vector", "ray", "tik", "tok", "ilo", "music",
        "wave", "signal", "tone", "pixel", "frame", "panel", "shell", "beam", "bank", "se", "ny", "be", "come", "zone", "hope", "try", "app", "vi",
        "pillar", "column", "row", "cell", "chart", "graph", "tree", "branch", "draft", "king", "kings", "queen", "queens", "n", "m",
        "leaf", "root", "source", "origin", "foundation", "capital", "center", "ship", "land", "medal", "med", "al",
        "heart", "mind", "brain", "nerve", "fiber", "matter", "metal", "iron", "face", "the",
        "copper", "silver", "gold", "platinum", "plat", "diamond", "dia", "crystal", "gem",
        "jade", "amber", "atlas", "globe", "glo", "world", "wo", "earth", "ear", "luna", "terra",
        "sol", "flare", "fla", "torch", "lamp", "laser", "plasma", "ma", "ion", "atom",
        "compound", "attack", "ack", "att", "at", "secure", "defense", "def", "ense", "pro", "via",
        "wave", "nexus", "quant", "ground", "ly", "ed", "s", "wonder", "globe",
        "global", "border", "sea", "open", "rate", "usd", "us", "deep", "mind", "trust", "pilot", "lot", "bid", "ask", "spread", "liquid",
        "head", "hand", "foot", "un", "get", "ultra", "coin", "chain", "dex", "info", "wars",
        "bean", "code", "rabbit", "increment", "strong", "pair", "load", "store", "shop", "up", "dir",
        "fix", "index", "idx", "feed", "fortress", "shift", "back", "weap", "wep", "on", "vest", "rest", "n",
        "pledge", "phys", "ical", "cur", "rent", "spark", "forge", "granite",
        "bin", "ary", "lyst", "drift", "cosmo", "chamber", "sphere", "polygon", "poly", "gon", "bond", "vis", "ta", "vista", "ist", "va",
        "canyon", "ridge", "archive", "ar", "ri", "cipher", "launch", "sync", "orbit", "orb", "bit",
        "summit", "canvas", "syntax", "module", "script", "tum", "float", "flo", "at", "oat", "o", "k", "g", "mb", "gb", "kb", "tre", "fair", "equi", "aqui", "aqua", "das", "alpha", "rev", "profit", "cap", "max", "up", "north", "south", "east", "west",
        "circuit", "gateway", "gate", "way", "inf", "up", "bet", "365", "247", "poly", "gon", "pix", "el", "fin", "fi", "find", "nd",
        "pattern", "pat", "atic", "diem", "fiat", "cash", "app", "para", "epi", "cent", "ter", "mono", "lith", "ev", "event", "eve", "ent", "entity", "work", "api", "dock", "docker", "cont", "container", "enter", "tain",
        "con", "stella", "equa", "eq", "tor", "watt", "hype", "r", "hyper", "lane", "rest", "int", "double", "er", "trade", "doubler", "ify", "doub",
        "tri", "ad", "carton", "car", "spec", "tra", "bib", "lio", "li", "i", "l", "blue", "red", "yellow", "green", "migrate",
        "cor", "tex", "te", "cas", "cade", "ar", "arcade", "cab", "net", "cabi", "col", "proto", "relay", "lay",
        "synth", "sys", "call", "put", "long", "bull", "et", "axi", "om", "epsi", "lon",
        "dim", "ta", "lambda", "top", "fast", "insta", "app", "ink",
        "dyna", "mic", "tor", "que", "wave", "length", "wa", "len", "circa", "cir", "ca", "mary",
        "spec", "amp", "li", "lexi", "con", "nome", "metro", "photon", "tick", "safe", "moon", "sun", "glow", "flow", "flux", "ux",
        "tron", "ton", "sev", "ere", "go", "d", "long", "lo", "cov", "vari",
        "scalar", "tensor", "formula", "tristate", "neur", "al", "neural",
        "swap", "ai", "fix", "pair", "pairs", "bee", "goose", "game", "sock", "snow", "flake", "wall", "sent", "ry", "sentry",
        "port", "tra", "for", "ori", "or", "org", "o", "inc", "c", "fire", "sea", "web", "street", "route", "road", "struct",
        "co", "ad", "ante", "bi", "circum", "cir", "contra", "con", "tra", "counter", "count", "de", "dis",
        "ex", "in", "im", "il", "ir", "infra", "inter", "intra", "int", "non", "sen", "senti", "ti", "se", "s", "e",
        "ob", "oc", "op", "per", "post", "pre", "pro", "re", "retro", "sub", "avail", "able", "available", "ava", "av", "a", "v", "ail", "p",
        "super", "supra", "trans", "ultra", "ult", "uni", "able", "ible", "ory", "play", "shadow", "ow", "shad",
        "arium", "orium", "or", "ary", "ic", "al", "ate", "itas", "tudo", "men", "", "req", "q", "r", "u", "v", "w", "x", "y", "z"
        "tion", "sion", "ment", "ure", "ive", "go", "fin", "find", "ind", "ex", "na", "daq", "deck", "big", "stable",
        "ana", "holo", "gram", "kilo", "mega", "exa", "yotta", "byte", "bit", "dog", "data",
        "hive", "ar", "weave", "link", "wallet", "cata", "tran", "epi", "cen", "ter", "a", "s", "tri", "bi", "quad", "penta", "state", "for", "est", "en", "in", "to", "fort", "ok", "so", "ed", "er", "ex",
        "mon", "mo", "no", "cross", "add", "more", "dev", "find", "match", "exe", "c", "no", "n", "feed", "back", "cur", "rent", "re", "amp", "ere"
    }

    max_length = max(len(w) for w in words)
    print(max_length)
    checker = DomainChecker(max_concurrent=1000, timeout=2.0)
    await checker.run(words)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Process terminated by user")
