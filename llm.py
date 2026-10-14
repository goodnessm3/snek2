import os.path
from collections import defaultdict
import datetime
from openai import AsyncOpenAI, BadRequestError
from collections import deque
import json
import asyncio
from utility import LeakyBuckets
import re
import hashlib
from discord.ext import commands
import datetime
from functools import wraps
from moderation import contains_forbidden

rl = chr(128680)  # emoji siren light character for moderation message
DISCORD_ID_FINDER = re.compile("<@[0-9]+>")
DISCORD_ID_EXTRACTOR = re.compile("<@([0-9]+)>")


def format_convo_log(ls, earliest):

    """Condense a log from the earliest date onwards. Earliest needs to be a datetime object. Returns a big
    block of text of user/agent interactions to pass to the memory consolidation mechanism."""

    out = ""

    for q in ls:
        t = datetime.datetime.fromisoformat(q["timestamp"])
        if t < earliest:
            continue

        if "message" in q.keys():
            k = "message"  # deprecated
        else:
            k = "content"

        out += f'''{q["role"]}: {q[k]}\n'''

    return out

def track_activity(func):

    """For use within the convo tracker class, to determine if we've been idle for a while"""

    @wraps(func)
    async def wrapper(self, *args, **kwargs):
        self.last_activity = datetime.datetime.now()
        return await func(self, *args, **kwargs)
    return wrapper

class FileHandleDict:

    """Use this for writing conversation logs to disk"""

    def __init__(self):

        self.dc = {}

    def log(self, uid, msg, role):

        if not uid in self.dc.keys():
            self.dc[uid] = open(f"memories/{uid}.log", "a", encoding="utf-8")

        fh = self.dc[uid]  # file handle

        d = {"timestamp":datetime.datetime.now().isoformat(),"content":msg,"role":role,"uid":uid}
        fh.write(json.dumps(d, ensure_ascii=False))
        fh.write("\n")
        fh.flush()

    def log2(self, uid, js):

        """For just dumping the message as it comes out of the API rather than rolling our own dict type thing"""

        fh = self.dc[uid]  # file handle
        js["timestamp"] = datetime.datetime.now().isoformat()
        fh.write(json.dumps(js, ensure_ascii=False))
        fh.write("\n")
        fh.flush()

    def close(self):

        for v in self.dc.values():
            v.close()
        print("Closed logger file handles.")

    def flush(self):

        for v in self.dc.values():
            v.flush()

class ConvoTracker(commands.Cog):

    def __init__(self, bot):

        self.bot = bot

        with open("secret/prompt.txt", "r") as f:
            prompt = f.read()

        with open("secret/llm_settings.json", "r") as f:
            js = json.load(f)  # use it later
            self.client = AsyncOpenAI(base_url=js["base_url"],
                                      api_key="local")  # api key not used but OpenAI library still wants it
            self.prompt = prompt
            self.llm_args = js["llm_args"]
            self.memory_llm_args = js["memory_llm_args"]  # different settings for memory consolidation

        with open("secret/badwords.txt", "r") as f:
            self.badwords = set([x.rstrip() for x in f.readlines()])

        with open("secret/user_specific_stub.txt", "r") as f:
            self.user_stub = f.read()

        with open("secret/make_aux_prompt_prompt.txt") as f:
            self.aux_instructions = f.read()

        self.tracking = defaultdict(lambda: deque(maxlen=12))  # record of {"role":x, "content":message} dicts
        self.times = {}  # record when the user last interacted
        self.moderated = LeakyBuckets(2, 10)  # no more than 2 moderation failures, decrement every 600s
        # record how many times a user trips the moderation filters. Temporarily disable function if they are too crazy.
        self.logger = FileHandleDict()
        self.semaphores = defaultdict(lambda: False)  # prevent spamming
        self.last_activity = datetime.datetime.now()  # consolidate memories/do other stuff when it's quiet

        self.scheduled_dreams = set()  # users who spoke to us since we last consolidated memories
        self.dream_task = asyncio.ensure_future(self.dream_loop())  # I don't understand any of this stuff ._.

    def reset_semaphores(self):

        for k in self.semaphores.keys():
            self.semaphores[k] =  False

    def cancel_tasks(self):

        self.dream_task.cancel()

    async def dream_loop(self):

        try:
            while True:
                await self.dream()
                await asyncio.sleep(1800)
        except asyncio.CancelledError:
            print("dream loop was stopped")

    def substitute_uids(self, astr):

        """Not currently used."""

        ids = DISCORD_ID_FINDER.findall(astr)
        if not ids:
            return astr  # nothing to do, the most frequent case
        mapping = {}
        for x in ids:
            numeric = DISCORD_ID_EXTRACTOR.search(x)
            #name = self.buxman.uid_to_screen_name(numeric.groups()[0])
            name = "Jonh"
            mapping[x] = name

        for k, v in mapping.items():
            if v:  # don't try to substitute a None, this might happen if the user isn't in the names table somehow
                astr = astr.replace(k, v)

        return astr

    def get_moderation(self, uid, message):

        """  # sweet summer child method:
                for word in message.split(" "):
                    if word.lower() in self.badwords:
                        print(f"message filtered due to bad word: {word}")
                        return True

                return False
        """

        result, match = contains_forbidden(message, self.badwords)
        if result:
            print("message filtered due to: ", match)
        return result


    def get_user_specific_prompt_data(self, uid):

        """Generates a list of all memory observations, superseded by the condensed aux prompt method"""

        out = self.user_stub  # basic instructions about how to use the memory bullet points

        if os.path.exists(f"memories/{uid}.memory"):
            with open(f"memories/{uid}.memory", "r", encoding="utf-8") as f:
                for line in f.readlines():  # one JSON object per line, a set of memories for a given timestamp
                    ljs = json.loads(line)
                    out += ljs["memories"]
                    out +=  "\n"

            return out

        return ""  # if no memories, nothing to add

    def get_condensed_aux_prompt(self, uid):

        if os.path.exists(f"memories/{uid}.consolidated"):
            with open(f"memories/{uid}.consolidated", "r", encoding="utf-8") as f:
                return self.user_stub + f.read()

        return ""

    async def forgotten_function(self, uid, func_name):

        forgor = ("The user has asked you for a function that is no longer available in version 2."
                  " inform them that the developers are working on updating the functionality.")

        base = [{"role": "system", "content": self.prompt + "\n" + self.get_condensed_aux_prompt(uid) + "\n" + forgor}]

        dummy = {"role": "user", "content": f"I'd like to use the {func_name} function from version 1.0."}
        base += [dummy]
        self.tracking[uid].append(dummy)

        baz = self.llm_args.copy()
        baz["temperature"] = 1.0  # try for less repetition???

        response = await self.client.chat.completions.create(messages=base,
                                                             user=uid,
                                                             **baz
                                                             )

        answer = response.choices[0].message.content

        self.tracking[uid].append({"role": "assistant", "content": answer})

        return answer

    @track_activity
    async def get_response(self, uid, cid, message):

        """Returns the answer, and the amount of prompt tokens and completion tokens it used."""

        #print(self.tracking[uid])  # we track each user's conversation separately. Chances of a "group conversation"
        # are low enough that we just won't bother

        #message = self.substitute_uids(message)  # replace discord mentions with the actual screen name
        # disabled for now, but if we want the bot to "know" about people, will be useful

        last_query = self.times.get(uid, None)
        now = datetime.datetime.now()

        if last_query:
            delta = now - last_query
            if delta.seconds > 900:
                self.tracking[uid].clear()  # it's been more than 900 seconds, start fresh

        self.times[uid] = now  # update when most recently used

        self.tracking[uid].append({"role": "user", "content": message})
        # push new message into deque, old one will be lost
        base = [{"role": "system", "content": self.prompt + "\n" + self.get_condensed_aux_prompt(uid)}]

        self.logger.log(uid, message, "user")

        msg = base + list(self.tracking[uid])

        try:
            response = await self.client.chat.completions.create(messages=msg,
                                                                 user=uid,
                                                                 **self.llm_args  # loaded in from json file
                                                                 )

            answer = response.choices[0].message.content
            #print(dir(response))
            #print(response.to_json())
            context_length = response.usage.total_tokens
            #print(response)
            #print(context_length)  # temporary, to track how much context is typically used
            pt = response.usage.prompt_tokens
            ct = response.usage.completion_tokens

        except BadRequestError:
            response = None
            answer = "The context got too long, please try again."
            pt = ct = 0
            for x in range(4):
                self.tracking[uid].popleft()  # pop the oldest 25 messages to cut down the context length

        response_check = self.get_moderation(None, answer)
        if response_check:  # someone tricked the bot into saying a bad word
            answer = "Filtered"

        self.tracking[uid].append({"role": "assistant", "content": answer})

        #self.logger.log(uid, answer, "agent")
        if response:
            self.logger.log2(uid, response.to_dict()["choices"][0]["message"])

        try:
            return answer, pt, ct
        except Exception as e:
            print(e)
            return "Something went wrong!", 0, 0

    async def get_moderated_response(self, uid, cid, message):

        if not self.semaphores[uid]:
            self.semaphores[uid] = True
        else:
            print(f"Ignoring a message from {uid} while still generating")
            return

        self.scheduled_dreams.add(uid)  # know that we should update memories for this user

        if self.moderated.check_user(uid):  # it returns True if user has exceeded the threshold
            return "Blocked for tripping the content filter too many times, try again later."

        if len(message) > 600:
            message = "The user tried to submit a very long message, please ask them to submit shorter ones."

        moderation = self.get_moderation(uid, message)
        # this is just a dumb check for "bad words"

        if moderation:
            st = f"{rl} Uh oh! Your message tripped the filter!"
            self.moderated.increment_user(uid)
            self.semaphores[uid] = False
            return st
        else:

            answer_tuple = await self.get_response(uid, cid, message)
            answer, pt, ct = answer_tuple

            self.semaphores[uid] = False
            return answer

    async def _temp(self, ctx, t):

        self.llm_args["temperature"] = float(t)
        await ctx.send(f"Set temperature to {t}")

    def been_idle(self):

        """Have we done nothing for the last 10 minutes?"""

        return datetime.datetime.now() - datetime.timedelta(minutes=10) > self.last_activity


    async def dream(self):

        """Consolidate memories and message logs and generate a prompt supplement specifically for each user"""

        if self.been_idle():
            if len(self.scheduled_dreams) > 0:
                uid = self.scheduled_dreams.pop()  # arbitrarily get a user who we spoke to recently
                await self.rember(uid)
                await self.consolidate(uid)
                print("dreamt")
            else:
                print("nothing to dream about")
        else:
            print("didn't dream - still busy!")


    async def rember(self, uid):

        print(f"consolidating memories for user {uid}")

        with open("secret/summarization_prompt.txt", "r") as f:
            sum_prompt = f.read()

        if os.path.exists(f"memories/{uid}.memory"):
            with open(f"memories/{uid}.memory", "r", encoding="utf-8") as f:
                for line in f.readlines():  # one JSON object per line, a set of memories for a given timestamp
                    l = line
                ljs = json.loads(l)
                last_updated = datetime.datetime.fromisoformat(ljs["timestamp"])
        else:
            last_updated = datetime.datetime.fromisoformat('1970-01-01T00:00:01.904954')

        with open(f"memories/{uid}.log", "r", encoding="utf-8") as f:
            convo_log = []  # this will be a list of JSON dicts

            for line in f.readlines():
                convo_log.append(json.loads(line))

            new_info = format_convo_log(convo_log, last_updated)  # big block of text to send to the LLM for summarizat


        messages = [{"role": "system", "content": sum_prompt},
                    {"role": "user", "content": f"begin chat transcript: {new_info}"}]

        try:
            response = await self.client.chat.completions.create(messages=messages,
                                                                 user="self",
                                                                 **self.memory_llm_args  # loaded in from json file
                                                                 )

            answer = response.choices[0].message.content  # bullet point list, an object of new memories

        except BadRequestError as e:
            print("error with memory consolidation")
            print(e)
            return

        out = {"timestamp":datetime.datetime.now().isoformat(),"memories":answer}
        with open(f"memories/{uid}.memory", "a", encoding="utf-8") as f:
            f.write(json.dumps(out))
            f.write("\n")

        print("memories written")

    async def consolidate(self, uid):

        todo = ""

        with open(f"memories/{uid}.memory", "r", encoding="utf-8") as f:
            for line in f.readlines():  # one JSON object per line, a set of memories for a given timestamp
                j = json.loads(line)
                todo += f'''{j["timestamp"]}\n{j["memories"]}\n'''

        print(todo)

        messages = [{"role": "system", "content": self.aux_instructions},
                    {"role": "user", "content": f"begin memories: {todo}"}]

        response = await self.client.chat.completions.create(messages=messages,
                                                             user="self",
                                                             **self.memory_llm_args  # loaded in from json file
                                                             )

        answer = response.choices[0].message.content  # bullet point list, an object of new memories

        with open(f"memories/{uid}.consolidated", "w", encoding="utf-8") as f:
            f.write(answer)  # we append this to the system prompt to tailor it for the specific user


async def setup(bot):

    await bot.add_cog(ConvoTracker(bot))
