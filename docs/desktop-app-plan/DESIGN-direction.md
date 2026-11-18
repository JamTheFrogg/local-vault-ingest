# Design direction: vault-ingest desktop app

Planning document. Two visual directions inside the owner's brief ("dark, calm, tool-like, close to Obsidian /
Linear"), one recommended. No UI code.

**Status.** This is the planning stage of the impeccable `new-work` playbook. That playbook picks a direction
with a random roll from a script (`concept-seed`); the brief forbids running the skill's programs, so the roll
did not happen and both directions below were derived by hand (`seed: none (unavailable)`). The final
`DESIGN.md` is written after the first build, from what was actually built, not from this file.

The surface mode is **Operate**. In that mode a visual world may lend four things and only these: **type,
palette, density and one signature move**. Layout, navigation and controls stay the standard ones described in
`03-ux-shape.md`, identical in both directions.

---

## 1. Ground work

**The mechanism, in one sentence.** A call goes in as sound and comes out as plain files: a note, a transcript
in parts, and an updated client profile, all in a folder the user owns.

**The scene.** A freelance developer at a desk, a wide monitor with a dark code editor on one side and a video
call on the other, often in the evening, sometimes with a game on the same machine later. *That scene is why
the app is dark: it sits beside dark tools and must not flare next to a call window.* The owner also fixed
dark; the scene agrees with him.

**What the first screen must prove.** That the meeting he just had is safe, is being worked on, and will be
readable in one place. So the first screen is the Meetings list with a live row, not a dashboard.

**The existing visual material (evidence, not authority).** The tkinter popup uses a blue-grey dark card
(`#1f2128`, field `#2a2d36`, text `#eceef3`, accent `#4c8dff`, record `#e5484d`). The tray uses grey for idle,
blue for processing and amber for paused. Both directions **keep those state meanings** (grey idle, blue
working, amber paused, red recording), because the owner has already learned them. The blue-grey itself is
replaced.

**The rut, kept out on purpose.**

- The category default: a near-black neutral grey ground, Inter, one violet or indigo accent, hairline borders
  at 8% white, and a graph of glowing dots. This is Linear and Obsidian averaged, without their polish.
- Its predictable opposite: a cream paper notebook with a serif headline.
- The literal reading of the name: vault doors, locks, safes, shields.

**Seven sources from the user's world, ordered by fit.** (Not tools he operates; in Operate mode those come
back as costumes.)

| # | Source | Why it fits | How it looks |
|---|---|---|---|
| 1 | Typed meeting minutes / the minute book | A call note is exactly this: who was there, what was decided, who does what by when | Text face for reading, a narrow margin for times and references, ruled sections |
| 2 | Magnetic tape and the record lamp (reel, cassette, dictaphone) | The product starts with a recording; "is it recording?" is the first question | Oxide brown-black, ivory label stock, one red lamp, a counter in fixed-width digits |
| 3 | A transit line map and its timetable | Clients are lines, meetings are stops in date order, shared notes are interchanges; it is a graph people already read | Dark ground, strong line colours, ring-shaped stops, condensed signage type, time columns |
| 4 | A court reporter's transcript | The transcript parts: time in the margin, speaker, exact words | Numbered or timed margin, fixed-width times, plain text face |
| 5 | A client file with index tabs | Everything about one client behind one tab | A colour per client, used as a small tab or swatch |
| 6 | A darkroom contact sheet | Screenshots taken during a meeting are frames along a strip, some of them marked | Frames in a row along a time strip, marks on the chosen ones |
| 7 | A ship's or observatory logbook at night | Timed entries, a calm night-time register | Ink-blue ground, timed lines |

These span five families (paper documents, audio hardware, information graphics, photography, logbooks).
Direction A fuses 2 with 1, 4 and 6. Direction B fuses 3 with 5 and 7.

---

## 2. Rules both directions share

- **Colour strategy: restrained, with a committed shell.** The rail and top bar carry a visibly tinted colour
  of their own; the content ground is a tinted near-black, never neutral grey and never pure black. One accent
  for primary action, selection and focus. Colour otherwise appears only where it means something.
- **State colours** (hue varies slightly per direction, meaning does not):

| Meaning | Colour | Always paired with |
|---|---|---|
| Recording | red (vermilion) | filled dot + timer or the word "Recording" |
| Working (transcribing, writing notes, downloading, importing) | blue / the accent | progress + stage name |
| Paused, waiting on a rule | amber | pause glyph + the reason |
| Done, imported, connected | green | tick + word |
| Failed, needs review | rose | exclamation + the reason |
| Idle, skipped, not found | muted neutral | word |

  Recording and failure both sit near red, so they are separated three ways: hue (orange-red against pink-red),
  shape (filled dot against exclamation mark), and wording.
- **Client colours** are a set of eight, given to clients in order and repeated after eight. A client colour
  only ever appears as a small swatch beside the client's name and as the colour of that client's nodes in the
  graph. It is never a state and never the only way to tell clients apart.
- **No**: glow, blur, glass, gradients on surfaces, gradient text, coloured stripes down the side of cards or
  rows, shadows on dark surfaces (borders and tone steps do that job), pure `#000` or `#fff`.
- **Selection** is a tone step plus primary-coloured text, never colour alone. **Focus** is a 2 px ring in the
  accent with a 2 px gap.
- **Motion** conveys state only. 120 ms for hover and press, 180 ms for drawers, popovers and popup changes,
  nothing longer. One easing, a plain ease-out (`cubic-bezier(0.2, 0, 0, 1)`); no bounce. No animation when the
  app or a section opens. Meters and progress bars are data and always move. With "reduce motion" on: no
  pulse, no slide, no animated graph layout.
- **Fonts are bundled with the app** (no network fetch), unmodified, with their licence files. Fallbacks are the
  system's own fonts.

---

## 3. Direction A: Oxide  (recommended)

**In one line.** A warm, quiet tool the colour of recording tape, where the note is set like typed minutes and
every meeting is drawn as a strip of tape with its marks on it.

**Thesis.** The app has two jobs with two rhythms: operating (lists, settings, the popup) and reading (the
note before a client meeting). Oxide gives each its own type voice and one warm ground under both. It refuses
the cool blue-grey that every dark tool ships.

### Palette

Contrast ratios are WCAG 2.x ratios, computed, against the surface named.

| Role | Colour | Contrast |
|---|---|---|
| Content ground | `#181513` | - |
| Shell (rail, top bar) | `#241C17` | 1.08 against the content ground; the edge is also a 1 px line |
| Raised (cards, popovers, inputs, the popup) | `#211C19` | - |
| Hover row | `#2B2520` | - |
| Selected row | `#3A2C22` | 1.35 against the content ground |
| Divider (decorative) | `#322B26` | 1.31 (decorative, no minimum) |
| Control border (inputs, checkboxes, outlined buttons) | `#75695F` | 3.41 on content, 3.17 on raised (needs 3.0) |
| Text, primary | `#EFE8DC` | 14.93 on content . 13.76 on shell . 13.85 on raised . 11.03 on selected |
| Text, secondary | `#B9AFA2` | 8.41 on content . 7.75 on shell . 6.22 on selected |
| Text, muted | `#968C80` | 5.50 on content . 5.07 on shell . 5.11 on raised . 4.58 on hover |
| Text, disabled | `#6B635B` | 3.08 on content (disabled text has no minimum) |
| Primary button | fill `#EFE8DC`, text `#181513` | 14.93 |
| Accent: links, focus ring, "working" | `#7FB2F0` | 8.25 on content . 7.60 on shell . 7.65 on raised . 6.10 on selected |
| Record button | fill `#C9341F`, text `#FFFFFF` | text 5.26 . fill 3.46 against content, 3.21 against raised |
| Recording dot and timer text | `#F4755C` | 6.53 on content . 6.06 on raised |
| Paused | `#E0A63A` | 8.38 on content . 7.73 on shell |
| Done | `#8CC07A` | 8.60 on content |
| Failed / needs review | `#F08BA0` | 7.71 on content . 7.15 on raised |

Client colours (dusty, so they sit below the state colours), each on the content ground:
teal `#5FAFA3` 7.05 . blue `#7C9FD6` 6.74 . lilac `#A891D6` 6.65 . pink `#CF87AE` 6.69 . clay `#D08A62` 6.49 .
ochre `#C2A85E` 7.84 . olive `#9DB06A` 7.66 . slate `#97A3AA` 7.04.

The primary action is ivory, not a hue. That leaves blue free to mean one thing ("the app is working, or this
is where you are") and red to mean one thing ("recording").

### Type

| Use | Face | Fallbacks |
|---|---|---|
| Interface: labels, lists, buttons, headings | **Atkinson Hyperlegible Next** | `system-ui, "Segoe UI", sans-serif` |
| Note body in the reader | **Literata** | `Charter, Georgia, serif` |
| Times, timers, paths, frontmatter, code | **Atkinson Hyperlegible Mono** | `ui-monospace, "Cascadia Mono", "SF Mono", Menlo, Consolas, monospace` |

- Why these: Atkinson Hyperlegible was drawn so that similar letters cannot be confused (I, l, 1; O, 0), which
  matters for meeting codes like `abc-defg-hij`, file names and small labels on a dark ground. Literata was
  made for long reading on screens and its stroke contrast is low enough to hold up as light text on dark. None
  of the three is one of the faces every generated interface uses.
- The serif is for note text only. Headings inside a note, and everything outside the reader, stay in the
  interface face. No italic display type, no small tracked capitals.
- Fixed sizes (px): 12 caption . 13 small . **14 base** . 16 emphasis . 19 title . 23 page title. Weights 400,
  500, 700 (the Mono is confirmed to ship 200-800; that the Next files hold the same weights is unverified).
- Reader: Literata 16 px, line height 1.6, about 68 characters per line. Transcript text the same, with the
  time of each turn in the left margin in the mono face at 13 px.
- Numbers in lists and timers use fixed-width digits so they do not jump.

### Spacing and density

Comfortable. 4 px base: 4, 8, 12, 16, 24, 32, 48. List rows 36 px for one line and 56 px for two. Controls 32 px
high, 36 px in the popup. Corner radius 6 px on controls, 8 px on cards and popovers. 1 px borders.

### Icons

**Phosphor**, Regular weight at 16 and 20 px; the Fill weight marks the current rail item and the recording
dot. Phosphor ships six weights, so emphasis comes from weight and not from added colour. Fallback set:
Lucide.

### Signature move: the tape strip

Every meeting is drawn as one horizontal strip scaled to its length, with ticks where transcript parts begin,
where screenshots were taken and where moments were marked.

- In the meeting detail it is the audio player and the way to jump to a part or a screenshot.
- In the popup, the level meters and the counter are the same strip while it is being written.
- In the reader, the times down the margin of a transcript are the same marks turned vertical.

It is drawn with plain lines and ticks. No reels, no needles, no bezel, no texture.

### Motion

The shared rules, plus: the playhead moves at constant speed (no easing, it is time). The recording dot fades
between 100% and 55% once every two seconds; it does not grow. A new mark on the strip appears in 120 ms.

### The graph

Warm and quiet. Links are 1 px lines in `#4A413A`. A client profile is a filled circle of 10 px radius in the
client's colour with a thin ivory ring, and its name is always shown in the interface face at 12 px. A meeting
is a 4-5 px dot in its client's colour. Other notes are small neutral diamonds. On hover the node and its
neighbours keep full strength and the rest drops to 25%; the neighbours' links turn ivory. A selected node gets
a 2 px ivory ring. No glow, no particles, no moving background; the layout settles and stops.

### First screen

Tinted brown rail on the left with the ivory-on-brown wordmark and the Record control at the top; the Meetings
list with client swatches; on the right a note in Literata under a header in the interface face, with the tape
strip between them. The only saturated thing on screen is whatever is recording.

### Why it will not look like a generic AI-made dashboard

There are no stat cards and no numbers in large type; the first screen is a list and a document. The ground is
brown-black, not blue-grey, and the main action is ivory instead of a violet or neon accent, so the usual
"near-black plus one bright colour" picture never forms. Two type voices do real work (operate, read) instead
of one geometric sans everywhere. The one drawn object, the tape strip, exists only because this product has
recordings with marks in them; it could not be pasted into another app. Colour is spent on five meanings and
eight client swatches and nowhere else, and nothing glows.

### Honest risk

Brown can turn muddy on a poorly calibrated or very bright monitor; if it does, the shell moves toward
`#201B18` and the selected row loses some red. A serif in a tool may feel unlike Obsidian to the owner; the
reader face can be switched to the interface face without touching anything else in the direction.

---

## 4. Direction B: Night Line

**In one line.** A cool, compact tool that looks like a transit map at night: each client is a coloured line,
each meeting a stop on it.

**Thesis.** The vault is a network, and people already know how to read one kind of network at a glance. Night
Line makes the client's line the organising colour of the whole app and sets everything in a condensed signage
type at high density. It refuses the graph as a cloud of identical dots.

### Palette

| Role | Colour | Contrast |
|---|---|---|
| Content ground | `#0F1719` | - |
| Shell (rail, top bar) | `#0D2229` (petrol) | 1.10 against the content ground; plus a 1 px line |
| Raised | `#162125` | - |
| Hover row | `#1B2A2F` | - |
| Selected row | `#17323B` | 1.34 against the content ground |
| Divider (decorative) | `#22343A` | 1.40 |
| Control border | `#5F7780` | 3.84 on content, 3.47 on raised |
| Text, primary | `#E4EEF0` | 15.37 on content . 13.92 on shell . 13.91 on raised . 11.43 on selected |
| Text, secondary | `#A7B9BE` | 8.93 on content . 8.08 on shell . 6.64 on selected |
| Text, muted | `#82979D` | 5.93 on content . 5.37 on shell . 5.37 on raised . 4.84 on hover |
| Text, disabled | `#566970` | 3.16 on content |
| Accent: primary button, links, focus, "working" | `#5EC4DC`; button text `#06181D` | 8.99 on content . 8.14 on shell . 6.69 on selected . button text 9.00 |
| Record button | fill `#D23A2A`, text `#FFFFFF` | text 4.80 . fill 3.79 against content, 3.43 against raised |
| Recording dot and timer text | `#FF7961` | 7.05 on content . 6.38 on raised |
| Paused | `#F0B43C` | 9.77 on content |
| Done | `#5CCB8E` | 8.98 on content |
| Failed / needs review | `#FF8FA6` | 8.41 on content . 7.61 on raised |

Line colours (saturated, because here they carry the structure), each on the content ground:
orange `#F2904A` 7.66 . lime `#9BCB4B` 9.54 . emerald `#3FBF8F` 7.83 . azure `#4AA3F0` 6.73 . violet `#9E8CF2`
6.47 . magenta `#E273C2` 6.45 . sand `#D8C27A` 10.30 . grey `#9AA7AD` 7.35.

Pure red and amber are left out of the line set so a line is never mistaken for "recording" or "paused".

### Type

| Use | Face | Fallbacks |
|---|---|---|
| Everything | **Mona Sans** (variable: weight 200-900, width 75-125) | `system-ui, "Segoe UI", sans-serif` |
| Times, timers, paths, code | **Mona Sans Mono** | `ui-monospace, "Cascadia Mono", "SF Mono", Menlo, Consolas, monospace` |

- Why: one family whose width can be narrowed. Normal width for reading; a narrower setting (about 88) for
  table columns, the popup and graph labels, which is how signage fits long names. No serif anywhere.
- Fixed sizes (px): 12 caption . **13 base** . 15 emphasis . 17 title . 20 page title . 24 rare. Weights 400,
  550, 700.
- Reader: 15 px, line height 1.55, about 68 characters per line.

### Spacing and density

Compact. 4 px base, same steps. List rows 30 px for one line and 48 px for two. Controls 28 px high, 36 px in
the popup. Corner radius 4 px on controls, 6 px on cards. 1 px borders.

### Icons

**Tabler Icons**, outline at 16 and 20 px with the stroke set to 1.5; its filled variants mark the current rail
item. Fallback set: Lucide.

### Signature move: the client line

- In the Meetings list, a thin route line runs down the gutter beside the rows, and each meeting is a ring on
  it in its client's colour.
- On a client page, the meetings of that client are stops on one line in date order, with the decision count
  as the stop's label.
- In the graph, links take the colour of the client they belong to.

### Motion

The shared rules, plus: a selected stop's ring fills in 120 ms; a new meeting's stop is drawn onto the line in
180 ms.

### The graph

Drawn like a line map. A link between a client and one of its meetings is 2 px in that client's line colour;
a link that crosses between clients is a thin grey dashed connector. A meeting is a ring: light fill, 2 px
stroke in the line colour. A client is a small name plate filled with its line colour, dark text. Other notes
are grey rings. Labels in the narrow width at 12 px. The layout is still the settle-and-stop layout of
`03-ux-shape.md`; only the drawing changes. No glow.

### First screen

Petrol rail, the Meetings list with a route line and rings down its left gutter, a dense detail pane in one
sans, the aqua primary button.

### Why it will not look like a generic AI-made dashboard

The colour on screen is structural: eight line colours that mean "which client", not one accent sprinkled on
buttons and charts. The graph reads as a map with named lines instead of a constellation. The type narrows
where space is tight instead of shrinking, which gives lists a signage rhythm that a default sans cannot. There
are no cards with rounded corners holding single numbers; the unit of the interface is a row on a line.

### Honest risk

It is the closer of the two to Linear, and a compact cool dark tool with an aqua accent can slide into the very
default it is trying to avoid if the line colours are used timidly. Eight saturated colours are less calm than
the brief asks for. With many clients the colours repeat and the map metaphor weakens. The route line in the
list is the hardest piece to build well.

---

## 5. Side by side, and the recommendation

| | A Oxide | B Night Line |
|---|---|---|
| Ground | warm brown-black | cool petrol-black |
| Primary action | ivory | aqua |
| Type | hyper-legible sans + reading serif + mono | one variable sans (width axis) + mono |
| Density | comfortable (14 px base, 36 px rows) | compact (13 px base, 30 px rows) |
| Icons | Phosphor (weights) | Tabler (stroke 1.5) |
| Signature | the tape strip (a meeting as a strip with marks) | the client line (clients as lines, meetings as stops) |
| Graph | quiet dots in dusty client colours | line map: coloured links, ring stops |
| Calm | higher | lower |
| Distance from Linear / Obsidian | further | nearer |
| Build effort | lower: the strip is one component used in three places | higher: the route line in lists and the map drawing |

**Recommendation: A, Oxide.** Three reasons. It is the calmer of the two, which is what the owner asked for.
Its signature is something the product needs anyway (an audio timeline with screenshots and marks), so it costs
almost nothing extra. And it treats reading as a first-class job, which is what the Monday-morning scene is.
If the serif turns out to be wrong for him, one setting replaces it and the rest stands.

B is worth keeping in the drawer for one idea: colouring graph links by client. That can be added to A later
as an option without changing its character.

---

## 6. Licences

Everything bundled must be redistributable from an MIT repository. Fonts under the SIL Open Font License may be
bundled with software under any licence as long as the fonts keep their own licence and copyright notice and
are not sold on their own (licence text: https://opensource.org/license/ofl/ , identifier:
https://spdx.org/licenses/OFL-1.1 ). They do not become MIT; they sit beside the code with their licence file.
The MIT and ISC icon sets need their copyright and licence notice kept.

| Asset | Used in | Licence | Checked how | Source |
|---|---|---|---|---|
| Atkinson Hyperlegible Next | A, interface | SIL OFL 1.1 | **Partly verified.** A third-party page reproduces the font's licence text (OFL 1.1, Braille Institute copyright, reserved names "Atkinson" and "Hyperlegible"); Wikipedia states OFL for the original family. Confirm the `OFL.txt` inside the actual download. | https://wiki.mobiusconsortium.org/wiki/Special:Version/License/The_Atkinson_Hyperlegible_Next_Font , https://en.wikipedia.org/wiki/Atkinson_Hyperlegible |
| Atkinson Hyperlegible Mono | A, mono | SIL OFL 1.1 | Verified on the font's package page (also: weights 200-800, italic). | https://fontsource.org/fonts/atkinson-hyperlegible-mono/about |
| Literata | A, reader | SIL OFL 1.1 | Verified in the project's repository (also: covers Greek, Cyrillic, Vietnamese). | https://github.com/googlefonts/literata |
| Mona Sans | B, interface | SIL OFL 1.1 | Verified in the project's repository (also: weight 200-900, width 75-125, italic). | https://github.com/github/mona-sans |
| Mona Sans Mono | B, mono | SIL OFL 1.1 | **Unverified for the Mono files specifically.** It is listed in the same repository, whose licence line names "Mona Sans". Confirm the licence file covers the Mono fonts. | https://github.com/github/mona-sans |
| Phosphor Icons | A | MIT | **Secondary source only.** Confirm the LICENSE file in the project's repository. | https://dev.to/svgicons/lucide-vs-tabler-vs-phosphor-which-free-icon-set-fits-your-ui-4ocl |
| Tabler Icons | B | MIT | **Secondary source only.** Same check. | same |
| Lucide | fallback in both | ISC | **Secondary source only.** Same check. | same |
| System fonts in the fallback lists | both | not bundled | nothing to license | - |

To do before any font or icon file enters the repository:

1. Open the licence file that ships with each download and keep it next to the files. Add a
   `THIRD-PARTY-LICENSES` file listing each asset, its copyright line and its licence.
2. Ship the font files as downloaded. The OFL lets a font reserve its name, and the Atkinson fonts do; whether
   cutting a font down to fewer characters counts as a modified version that must be renamed is **unverified**.
   Not modifying them avoids the question.
3. **Check the letters č ć đ š ž (and Č Ć Đ Š Ž) in every chosen font**, in every weight used. Coverage of
   these letters is **unverified** for Atkinson Hyperlegible Next, Atkinson Hyperlegible Mono and Mona Sans.
   Test string: `Čaušević, Đorđe, Šehić, Žunić`. Literata's coverage of Cyrillic is confirmed; for the other
   fonts, Cyrillic names fall back to the system font.
4. The app icon and the tray glyph do not exist yet and have to be drawn. They are the owner's own asset and
   need no third-party licence.
