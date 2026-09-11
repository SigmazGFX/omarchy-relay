"""Drawings for /ascii <what to draw>: a built-in set of small ASCII
drawings for common subjects, and big figlet letters for anything else.

Only the sender draws; the finished drawing goes out as an ordinary
"format": "ascii" chat message, so nobody else needs any of this.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from typing import Optional

_MAX_LETTERS = 40  # figlet output gets unwieldy well before this

# Each drawing starts with a "== name alias alias" line; an underscore in an
# alias stands for a space ("birthday_cake").
_DRAWINGS = r'''
== cat kitty kitten
 /\_/\
( o.o )
 > ^ <
== dog puppy doggo
  __      _
o'')}____//
 `_/      )
 (_(_/-(_/
== bunny rabbit
(\_/)
(o.o)
(")(")
== mouse rat
<:3 )~~~~
== owl
 ,_,
(O,O)
(   )
-"-"-
== duck
   __
 <(o )___
  ( ._> /
   `---'
== penguin
   __
  ( o>
  ///\
  \V_/_
== fish
        o
      o
  ><((((o>
== whale
       .
      ":"
    ___:____     |"\/"|
  ,'        `.    \  /
  |  O        \___/  |
~^~^~^~^~^~^~^~^~^~^~^~^~
== cow
        (__)
        (oo)
  /------\/
 / |    ||
*  /\---/\
   ~~   ~~
== spider
 / _ \
\_\(_)/_/
 _//"\\_
  /   \
== butterfly
 __   __
(  \,/  )
 \_ | _/
 (_/ \_)
== frog
  @..@
 (----)
( >__< )
^^ ~~ ^^
== heart love
 .:::.   .:::.
:::::::.:::::::
:::::::::::::::
':::::::::::::'
  ':::::::::'
    ':::::'
      ':'
== star
     .
    ,O,
   ,OOO,
'oooOOOOOooo'
  `OOOOOOO`
  OOO' 'OOO
 O'       'O
== sun sunny sunshine
    \   |   /
     .-"""-.
 -- /       \ --
    \       /
     '-...-'
    /   |   \
== moon night
   *  .  *
 .  __   .
   /  )
  |  (    *
   \__)
 *   .   .
== cloud cloudy
   .--.
.-(    ).
(___.__)__)
== rain rainy
   .--.
.-(    ).
(___.__)__)
 ' ' ' ' '
' ' ' ' '
== flower rose
   _
 _(_)_
(_)@(_)
 /(_)
 \|
  |/
== tree pine christmas_tree
     ^
    ^^^
   ^^^^^
  ^^^^^^^
 ^^^^^^^^^
    |||
== cactus
     _
   _| |
  | | | _
  | | || |
   \  | /
    | |
 ___|_|___
== mushroom
   .-"""-.
  / o   o \
 '---------'
     | |
     |_|
== coffee latte espresso tea
   ( (
    ) )
  ........
  |      |]
  \      /
   `----'
== beer pint
   .~~~~.
   i====i_
   |cccc|_)
   |cccc|
   `-==-'
== cocktail martini
 \~~~~~~/
  \    /
   \  /
    ||
    ||
  __||__
== cake birthday birthday_cake
     , , ,
    _|_|_|_
   {~*~*~*~}
 __{*~*~*~*}__
`-------------`
== pizza
 // ""--.._
||  (_)  _ "-._
||    _ (_)    '-.
||   (_)   __..-'
 \\__..--""
== rocket spaceship
     /\
    /  \
   | () |
   |    |
  /| /\ |\
 /_|/  \|_\
    ****
     **
== car
    ______
 __/__|__|\___
|  _     _   _|
'-(_)---(_)--'
== boat ship sailboat
      |\
      | \
      |  \
      |___\
 \--||___\_/
  \_______/
~~~~~~~~~~~~~~
== house home
     ______
    /      \
   /________\
   | []  [] |
   |   __   |
   |__|  |__|
== computer pc laptop
 .----------.
 |  >_      |
 |          |
 '----------'
   __|__|__
  [________]
== music song notes
    |~~~~~~~~|
    |~~~~~~~~|
    |        |
/~~\|    /~~\|
\__/     \__/
== trophy winner cup
  ___________
 '._==_==_=_.'
 .-\:      /-.
| (|:.     |) |
 '-|:.     |-'
   \::.    /
    '::. .'
      ) (
    _.' '._
   '-------'
== sword
      /\
      ||
      ||
      ||
    o====o
      ||
      []
== bomb boom
      ,--.!,
   __/   -*-
 ,d08b.  '|`
 0088MM
 `9MMP'
== skull skeleton
   _____
  /     \
 | () () |
  \  ^  /
   |||||
== ghost boo spooky
   .-.
  (o o)
  | O \
   \   \
    `~~~'
== robot bot
   [___]
  [ o o ]
  [  -  ]
 /|_____|\
   || ||
== alien
   .-""""-.
  /  _  _  \
 |  (o)(o)  |
  \   __   /
   '.____.'
== snowman snow winter
    _===_
    (o o)
   ( : )
  (  :  )
 ~~~~~~~~~
== smiley smile happy
  .-----.
 / o   o \
|    >    |
 \ `---' /
  '-----'
== shrug
¯\_(ツ)_/¯
== tableflip table_flip flip
(╯°□°)╯︵ ┻━┻
'''

_FILLER = {"a", "an", "the", "some", "my", "your", "of"}


class AsciiArtError(Exception):
    pass


def _parse(source: str) -> tuple[dict[str, str], tuple[str, ...]]:
    drawings: dict[str, str] = {}
    subjects: list[str] = []
    names: list[str] = []
    lines: list[str] = []

    def finish() -> None:
        while lines and not lines[-1].strip():
            lines.pop()
        for name in names:
            drawings[name] = "\n".join(line.rstrip() for line in lines)

    for line in source.split("\n"):
        if line.startswith("== "):
            if names:
                finish()
            names = [alias.replace("_", " ") for alias in line[3:].split()]
            subjects.append(names[0])
            lines = []
        elif names:
            lines.append(line)
    if names:
        finish()
    return drawings, tuple(subjects)


_BY_NAME, SUBJECTS = _parse(_DRAWINGS)


def lookup(subject: str) -> Optional[str]:
    """The built-in drawing for a subject, forgiving about case, punctuation,
    "a"/"the", and plurals ("Cats!", "a kitten"). None when there isn't one."""
    words = [word for word in re.findall(r"[a-z0-9]+", subject.lower()) if word not in _FILLER]
    phrase = " ".join(words)
    for candidate in (phrase, phrase[:-1] if phrase.endswith("s") else None):
        if candidate and candidate in _BY_NAME:
            return _BY_NAME[candidate]
    return _BY_NAME.get(subject.strip())  # the few subjects that aren't words, like "¯\_(ツ)_/¯"


def big_letters(text: str) -> str:
    if not shutil.which("figlet"):
        raise AsciiArtError(
            f"There's no built-in drawing of \"{text}\", and big letters need figlet "
            "(sudo pacman -S figlet). /ascii on its own lists the built-in drawings."
        )
    if len(text) > _MAX_LETTERS:
        raise AsciiArtError(f"That's too long for big letters (up to {_MAX_LETTERS} characters).")
    try:
        result = subprocess.run(["figlet", "-w", "80"], input=text, capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AsciiArtError(f"figlet failed: {exc}") from exc
    lines = [line.rstrip() for line in result.stdout.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    if result.returncode != 0 or not lines:
        raise AsciiArtError(f"figlet failed: {result.stderr.strip() or 'no output'}")
    return "\n".join(lines)


def draw(subject: str) -> str:
    """A built-in drawing of `subject`, or the words themselves in big letters."""
    return lookup(subject) or big_letters(subject.strip())
