import unittest

from matcher import choose_audio_edition, parse_storygraph_editions


EDITIONS_HTML = """
<main>
  <section class="edition-card">
    <a href="/books/a810eeff-d332-4b69-a85b-9fb99d2c4936">Teresa: Everybody loves large chests (Vol.5)</a>
    <div>missing page info • 2020</div>
    <div>ISBN/UID: None</div>
    <div>Format: Not specified</div>
    <div>Language: English</div>
    <div>Publisher: Not specified</div>
  </section>
  <section class="edition-card">
    <a href="/books/9cfd1ec8-aaaa-4ca4-ae55-287709244aaa">Teresa</a>
    <div>424 pages • digital • 2020</div>
    <div>ISBN/UID: None</div>
    <div>Format: Digital</div>
    <div>Language: English</div>
    <div>Publisher: Not specified</div>
  </section>
  <section class="edition-card">
    <a href="/books/36c06d90-2a99-4042-99ea-27435701f9dc">Teresa</a>
    <div>14h 52m • audio • 2020</div>
    <div>ISBN/UID: B08X18TDFQ</div>
    <div>Format: Audio</div>
    <div>Language: English</div>
    <div>Publisher: Soundbooth Theatre</div>
  </section>
  <!-- StoryGraph currently renders a second copy for another responsive layout. -->
  <section class="edition-card mobile">
    <a href="/books/36c06d90-2a99-4042-99ea-27435701f9dc">Teresa</a>
    <div>14h 52m • audio • 2020</div>
    <div>ISBN/UID:</div><div>B08X18TDFQ</div>
    <div>Format:</div><div>Audio</div>
    <div>Language:</div><div>English</div>
    <div>Publisher:</div><div>Soundbooth Theatre</div>
  </section>
</main>
"""


class MatcherTests(unittest.TestCase):
    def test_parses_and_deduplicates_storygraph_editions(self):
        editions = parse_storygraph_editions(EDITIONS_HTML)

        self.assertEqual(3, len(editions))
        audio = next(edition for edition in editions if edition.is_audio)
        self.assertEqual("36c06d90-2a99-4042-99ea-27435701f9dc", audio.book_id)
        self.assertEqual(892.0, audio.duration_minutes)
        self.assertEqual("B08X18TDFQ", audio.identifier)

    def test_selects_audio_edition_by_runtime(self):
        editions = parse_storygraph_editions(EDITIONS_HTML)

        match = choose_audio_edition(editions, target_duration_minutes=893.5)

        self.assertIsNotNone(match)
        self.assertEqual("36c06d90-2a99-4042-99ea-27435701f9dc", match.book_id)

    def test_refuses_a_large_runtime_mismatch(self):
        editions = parse_storygraph_editions(EDITIONS_HTML)

        match = choose_audio_edition(editions, target_duration_minutes=600)

        self.assertIsNone(match)

    def test_exact_identifier_takes_priority(self):
        editions = parse_storygraph_editions(EDITIONS_HTML)

        match = choose_audio_edition(
            editions,
            target_duration_minutes=600,
            identifiers=["b08x-18tdfq"],
        )

        self.assertIsNotNone(match)
        self.assertEqual("36c06d90-2a99-4042-99ea-27435701f9dc", match.book_id)


if __name__ == "__main__":
    unittest.main()
