# News -> Focus -> Event pipeline

This file defines the universal conversion rule used in OWW content authoring:

1. Any RP/news message is represented as a focus for the source country.
2. Focus completion must trigger a country event for the target country or global feed.
3. Every country has four baseline hooks in `OWW_all_tags_news_hooks.txt`:
   - advisor transfer
   - field exercises
   - alternative path
   - secret path
4. Alternative outcomes are implemented through `common/decisions/oww_alternative_outcomes.txt`.

Example pattern:
- News: "Obosslandia sent an advisor to Novraniya"
- Focus: `OBS_news_send_advisor_to_NOV`
- Event trigger on completion: `oww_news_hooks.1`
