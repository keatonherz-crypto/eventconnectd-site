# Brand Guidelines Document

_Started 2026-06-24 02:17 UTC_

---

## User

<system-info comment="Only acknowledge these if relevant">
Project title is now "Untitled"
Project currently has 1 file(s)
Current date is now June 23, 2026
</system-info>

<attached_files>
- uploads/eventconnectd-homepage-redesign-docs.pdf
</attached_files>

<!-- The user explicitly selected the following skills for this project, as attachments to their message. These are not optional context — they define how you work. Use them. -->
<attached-skill name="Design Components">
This project uses Design Components: every design is a single streaming `Name.dc.html` file. The full authoring spec is in your system prompt under "Writing code — Design Components" — follow it. Author and edit `.dc.html` content with the `dc_write`, `dc_html_str_replace`, `dc_js_str_replace`, and `dc_set_props` tools (not `write_file`; `str_replace_edit` works but won't stream); template edits stream into the live preview as you type.
</attached-skill>

<attached-skill name="Design System (design system)">
[Design System] This project uses the **Design System** design system. This is a binding choice for visual style — every visual must follow it. Don't invent colors, type, spacing, or components not grounded here.

Scope: the design system is a visual style reference only. Its guide may describe example products, brands, or people that are unrelated to the user and unrelated to the subject of this conversation. Never treat anything in the design system as a fact about the user, their work, or the topic they asked about.

    Explore it to find what you need:
    - Always copy out the fonts and colors you need
    - For prototypes and designs, always copy out any relevant components
    - If the design system contains existing mocks of products, and you were asked to design or prototype something similar, copy and fork those mocks to start your design. This helps you make high-quality designs.

    Explore it quickly to find relevant UI kits (e.g. mocks of existing products you can copy and fork)

Full system at `/projects/019e009b-5364-750b-bbc5-6540837e79b3/`. Before producing any visuals, explore it: call `list_files("/projects/019e009b-5364-750b-bbc5-6540837e79b3/")` to see the structure, then `read_file` the README/base.md or whatever index file it has. Don't guess at the design system's contents.

CSS tokens: the guide may describe tokens in prose, but the exact `--*` names are defined in the design system's stylesheet(s). Before writing any `var(--*)`, look up the real name in the design system's `.css` files (`list_files` + `read_file` under `/projects/019e009b-5364-750b-bbc5-6540837e79b3/`). Never guess a token name — an unresolved `var()` silently falls back to the browser default.

For assets and UI kits beyond the guide: `read_file("/projects/019e009b-5364-750b-bbc5-6540837e79b3/<path>")` and `copy_files` to bring them into the current project.
</attached-skill>

## Assistant

I'll start by exploring the uploaded brief and the design system.

_[tool: list_files]_

_[tool: read_file]_

The design system appears empty and the PDF needs proper handling. Let me check both.

_[tool: list_files]_

_[tool: read_skill_prompt]_

The design system project is empty. Let me read the PDF brief.

_[tool: run_script]_

## User

here is brand guidelines as well https://claude.ai/design/p/49affa46-892c-4b70-9f04-d583d8fa8ad6?file=Brand+Guide.dc.html

## Assistant

Let me read the brand guide and the full PDF brief.

_[tool: read_file]_

_[tool: run_script]_

Brief and brand guide are fully detailed with verbatim copy. Let me copy the brand assets, then build the homepage.

_[tool: list_files]_

_[tool: copy_files]_

_[tool: copy_starter_component]_

Now building the full 9-section homepage as one DC.

_[tool: dc_write]_

_[tool: ready_for_verification]_

Built the full Event ConnecTD homepage — all 9 sections from the brief (nav, hero, service tabs, who/why, value props + platform preview, interactive savings calculator, how-it-works, footer CTA, footer), using the navy/teal brand system, Playfair + DM Sans, and verbatim copy.

A few notes:
- The **hero image** and platform preview are placeholders — drag in a real TD/event photo for the hero.
- The **savings calculator** is live: four sliders drive fee-savings and time-value bars plus a running total.

## User

Found issues — fixing…

## Assistant

_[tool: dc_html_str_replace]_

_[tool: grep]_

_[tool: read_file]_

_[tool: str_replace_edit]_

_[tool: ready_for_verification]_

Fixed — the stray image-slot is gone; the loader now lives as a `<script>` in `<helmet>`, and the hero slot upgrades cleanly.

## User

do you have stock images for my hero

## Assistant

I don't have a built-in stock photo library, but let me check what's available in your design system and brand-guide project.

_[tool: list_files]_

_[tool: list_files]_

_[tool: view_image]_

Short answer: no — I don't have a stock photo library, and your design system project is empty. The only imagery in your brand assets is the logo, the mark, a screenshot, and that **LinkedIn banner** (the abstract blue/green wave graphic above).

So your realistic options for the hero:

1. **You provide a photo** — drop a real shot (a TD on a conference floor, an incentive trip, a team on-site) onto the placeholder slot and it persists. Best for trust/credibility.
2. **Use the brand wave graphic** — I can pull that LinkedIn banner's abstract wave aesthetic into the hero as a styled background instead of a photo. On-brand, no people, works immediately.
3. **Keep it illustrative** — I build a polished "platform preview" mock (the matched-TD UI) as the hero visual instead of a photo — shows the product, no stock needed.

Want me to go with **#2** (brand waves) or **#3** (product mock) so the hero looks finished right now? Or are you able to source a photo for #1?

If you want true stock photography, I'd suggest grabbing a couple of free shots from Unsplash/Pexels (search "corporate event coordinator" or "conference staff") and uploading them — then I'll place and color-treat them properly.

## User

how would this look for hero:

## Assistant

That's a great fit — networking, handshake, event setting, warm and human. It'll read perfectly against the navy hero. Let me wire it in.

_[tool: read_file]_

_[tool: dc_html_str_replace]_

_[tool: ready_for_verification]_

_[tool: save_screenshot]_

_[tool: save_screenshot]_

The photo looks great in the hero — warm, human, and the navy gradient overlay ties it into the brand. The "Matched in 48 hours" badge anchors the bottom corner. It's live in your preview.

## User

would it look better if there was a background animation like: https://www.egrinternational.com/

## Assistant

Quick take: EGR's hero uses a slow, ambient motion background (drifting gradient/particles) behind the content. That *can* add polish — but the key is restraint. A busy animation behind a photo + text would fight for attention and tip into "slop." 

The right move here: keep your photo, and add **subtle ambient motion to the navy area** — slow-drifting gradient glow + a few floating particles and the existing decorative rings gently breathing. It reads premium without distracting from the copy.

Let me add it (CSS-driven, behind the content, respects reduced-motion).

_[tool: read_file]_

Let me add the keyframes to the helmet and the animated layers to the hero.

_[tool: str_replace_edit]_

_[tool: str_replace_edit]_

_[tool: ready_for_verification]_

_[tool: save_screenshot]_

Added — the navy hero now has slow ambient motion: two soft drifting glow blobs (teal + blue), the decorative rings gently breathing, and a few floating particles. It's subtle by design, so the copy and photo stay the hero; the motion just adds depth. It pauses automatically for users with reduced-motion enabled.

## User

make it less subtle

## Assistant

_[tool: str_replace_edit]_

_[tool: str_replace_edit]_

_[tool: save_screenshot]_

Done — cranked it up: bigger and brighter teal/blue glow blobs (now three), faster drift, wider-pulsing rings, and more particles with glowing halos. The motion is clearly visible now rather than ambient. It's live in your preview — the static screenshot doesn't capture the movement, so give it a look.

## User

far less subtle, try something that would win awards

## Assistant

Love it — and there's a perfect thematic angle here. Your brand is about *connecting* planners with travel directors. So the award-worthy move is a **living network constellation**: glowing nodes drifting through the navy, linking with light-lines as they near each other, and reacting to the cursor. It literally visualizes "connection" — striking and on-message.

Let me build it on canvas.

_[tool: str_replace_edit]_

Now the canvas animation logic:

_[tool: dc_js_str_replace]_

_[tool: show_html]_

This is a visual animation I need to verify directly.

_[tool: save_screenshot]_

The screenshot tool re-renders the DOM and doesn't capture live canvas pixels — so the network won't show here even if it's running. Let me verify it's actually drawing.

_[tool: eval_js]_

_[tool: eval_js]_

_[tool: show_html]_

_[tool: eval_js]_

Confirmed — the canvas is live and drawing (lit pixels present). The screenshot tool can't capture canvas pixels, but it's running in your preview.

You now have an award-style **interactive network constellation** in the hero: ~teal and sky-blue glowing nodes drift through the navy, auto-linking with light-lines as they cluster, and **the whole field reacts to your cursor** — nodes lean toward the pointer and shoot connection lines to it, visualizing your "ConnecTD" brand idea. It auto-disables for reduced-motion users.

Move your mouse across the hero in the live preview to see the interaction.

## User

For rfp ps and wm those are the three products we offer, they either submit an rfp for tds to bid to get the job, personal selection where they view all our tds and select, or we manage, as in we currate the whole team, all you do is contract and pay them

## Assistant

Got it — those are three distinct service tiers, not job types. Let me fix the tab cards to reflect what each actually is.

_[tool: dc_js_str_replace]_

Let me also update that callout box next to the heading — "We Manage Highlighted TDs" now collides with the W.M. product name.

_[tool: dc_html_str_replace]_

Updated. The three service cards now read correctly:

- **RFP** — post the program, TDs bid, you award the winning proposal.
- **P.S. (Personal Selection)** — browse the full roster and hand-pick TDs yourself.
- **W.M. (We Manage)** — we curate the whole team end-to-end; you just contract and pay.

And the callout reframes them as "choose your level of control."

