import unittest
from dataclasses import replace

from matcher import (
    AudiobookDetails, EditionCandidate, edition_checks, match_audio_edition,
    merge_editions, parse_filtered_editions, parse_storygraph_editions,
)


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

        match, _ = match_audio_edition(editions, target_duration_minutes=893.5)

        self.assertIsNotNone(match)
        self.assertEqual("36c06d90-2a99-4042-99ea-27435701f9dc", match.book_id)

    def test_refuses_a_large_runtime_mismatch(self):
        editions = parse_storygraph_editions(EDITIONS_HTML)

        match, _ = match_audio_edition(editions, target_duration_minutes=600)

        self.assertIsNone(match)

    def test_exact_identifier_takes_priority(self):
        editions = parse_storygraph_editions(EDITIONS_HTML)

        match, _ = match_audio_edition(
            editions,
            target_duration_minutes=600,
            identifiers=["b08x-18tdfq"],
        )

        self.assertIsNotNone(match)
        self.assertEqual("36c06d90-2a99-4042-99ea-27435701f9dc", match.book_id)


    def test_each_outcome_says_which_rule_decided_it(self):
        editions = parse_storygraph_editions(EDITIONS_HTML)
        paperback = [edition for edition in editions if not edition.is_audio]

        def code(candidates, minutes, identifiers=None):
            return match_audio_edition(candidates, target_duration_minutes=minutes, identifiers=identifiers)[1]["code"]

        self.assertEqual("identifier", code(editions, 600, ["B08X18TDFQ"]))
        self.assertEqual("runtime", code(editions, 893.5))
        self.assertEqual("runtime_mismatch", code(editions, 600))
        self.assertEqual("no_results", code([], 600))
        self.assertEqual("no_audio", code(paperback, 600))
        self.assertEqual("only_audio", code(editions, 0))

    def test_a_runtime_mismatch_reports_how_far_off_the_closest_edition_was(self):
        editions = parse_storygraph_editions(EDITIONS_HTML)
        _, reason = match_audio_edition(editions, target_duration_minutes=600)
        self.assertEqual(292.0, reason["closest_delta_minutes"])
        self.assertEqual(12.0, reason["tolerance_minutes"])
        self.assertFalse(reason["abs_has_identifier"])

    def test_without_an_abs_runtime_two_audio_editions_cannot_be_told_apart(self):
        twins = [
            EditionCandidate("a" * 36, "Project Hail Mary", "Audio", 970.0, "B08GB58KD5", "English", None),
            EditionCandidate("b" * 36, "Project Hail Mary", "Audio", 970.0, "B08GB2RLKM", "English", None),
        ]
        match, reason = match_audio_edition(twins, target_duration_minutes=0)
        self.assertIsNone(match)
        self.assertEqual("no_abs_runtime", reason["code"])
        match, reason = match_audio_edition(twins, target_duration_minutes=970.9)
        self.assertEqual("runtime", reason["code"])
        self.assertEqual(1, reason["others_within_tolerance"])


    def test_parses_narrators_from_the_contributor_credits(self):
        html = """
        <section>
          <a href="/books/7112fac3-ec7f-4ead-8bc7-7b0d3a990da4">Project Hail Mary</a>
          <p><a href="/authors/1">Andy Weir</a>
            <span class="contributor-names">with <a href="/authors/2">Ray Porter</a> (Narrator),
            <a href="/authors/3">Frank van der Knoop</a> (Translator)</span></p>
          <div>16h 10m • audio • 2021</div>
          <div>ISBN/UID: B08GB58KD5</div><div>Format: Audio</div>
          <div>Language: English</div><div>Publisher: Audible Studios</div>
        </section>"""
        [edition] = parse_storygraph_editions(html)
        self.assertEqual(("Ray Porter",), edition.narrators)

    def test_the_work_link_and_read_another_edition_links_never_borrow_a_cards_details(self):
        # The real page's shape: a link to the work above the list, and once
        # you've read one edition, every other card links back to it.
        read = "b5de55e5-9dbb-4d6b-a5ce-215d728f823a"
        html = f"""
        <div class="page">
          <h1><a href="/books/{read}">Half the World</a></h1>
          <div class="current">
            <a href="/books/{read}">Half the World</a>
            <p>13h • audio • 2015</p>
            <div>ISBN/UID: 9781664422957</div><div>Format: Audio</div>
          </div>
          <div class="card">
            <a href="/books/{read}">You've read another edition</a>
            <a href="/books/b46886e6-0000-0000-0000-000000000000">Half the World</a>
            <p>Shattered Sea #2</p><p>366 pages • hardcover • 2015</p>
            <div>ISBN/UID: 9780804178426</div><div>Format: Hardcover</div>
          </div>
          <div class="card">
            <a href="/books/{read}">You've read another edition</a>
            <a href="/books/e341e9d7-0000-0000-0000-000000000000">Half the World</a>
            <p><a href="/authors/1">Joe Abercrombie</a>
              <span class="contributor-names">with <a href="/authors/2">John Keating</a></span></p>
            <p>13h • audio • 2015</p>
            <div>ISBN/UID: 9781470394011</div><div>Format: Audio</div>
          </div>
        </div>"""
        by_id = {edition.book_id[:8]: edition for edition in parse_storygraph_editions(html)}

        self.assertEqual({"b5de55e5", "b46886e6", "e341e9d7"}, set(by_id))
        self.assertEqual(("Audio", 780.0, "9781664422957"),
                         (by_id["b5de55e5"].format, by_id["b5de55e5"].duration_minutes, by_id["b5de55e5"].identifier))
        self.assertEqual(("Hardcover", None), (by_id["b46886e6"].format, by_id["b46886e6"].duration_minutes))
        # A whole number of hours, and a narrator credited with no role.
        self.assertEqual(780.0, by_id["e341e9d7"].duration_minutes)
        self.assertEqual(("John Keating",), by_id["e341e9d7"].narrators)
        # And the read-another-edition links say which edition is yours.
        self.assertEqual({"b5de55e5"}, {key for key, edition in by_id.items() if edition.read_by_you})

    def test_an_edition_you_have_read_on_a_later_page_is_kept_as_a_bare_id(self):
        read = "99999999-0000-0000-0000-000000000000"
        html = f"""
        <div class="card">
          <a href="/books/{read}">You've read another edition</a>
          <a href="/books/e341e9d7-0000-0000-0000-000000000000">Half the World</a>
          <p>13h • audio • 2015</p><div>ISBN/UID: 1</div><div>Format: Audio</div>
        </div>"""
        read_edition = next(e for e in parse_storygraph_editions(html) if e.read_by_you)
        self.assertEqual((read, "", False), (read_edition.book_id, read_edition.format, read_edition.is_audio))

    def test_parses_the_audio_filters_jquery_response(self):
        # The shape /filter-editions answers with: the cards as escaped HTML in
        # both branches of an if, and a "more..." link to the next page.
        card = (
            r'<div class=\"book-pane\"><a href=\"\/books\/c7cd14d0-0000-0000-0000-000000000000\">'
            r'Project Hail Mary<\/a><p>16h 11m • audio • 2021<\/p>'
            r'<p>It\'s narrated by <a href=\"\/authors\/2\">Ray Porter<\/a> (Narrator)<\/p>'
            r'<div>ISBN/UID: 9781713630296<\/div><div>Format: Audio<\/div><\/div>'
        )
        script = (
            "if (true) {\n  if ($('.panes').length > 0) {\n"
            f"    $('.panes').append(\"{card}\");\n"
            "    $('#infinite-scroll').replaceWith('<div><a id=\\\"next_link\\\" data-remote=\\\"true\\\" "
            "href=\\\"/filter-editions?book_id=x&format_audio=true&commit=Filter&page=2\\\">more...<\\/a></div>');\n"
            f"  }} else {{\n    $('.results').replaceWith(\"<span>{card}</span>\");\n  }}\n}}\n"
        )
        editions, next_page = parse_filtered_editions(script)
        self.assertEqual(2, next_page)
        [edition] = editions
        self.assertEqual(("9781713630296", 971.0, ("Ray Porter",)),
                         (edition.identifier, edition.duration_minutes, edition.narrators))

    def test_the_last_filter_page_has_no_next_page(self):
        self.assertEqual(([], None), parse_filtered_editions("if (true) { $('.panes').append(\"\"); }"))

    def test_merging_pages_keeps_the_fullest_copy_and_whether_you_read_it(self):
        bare = EditionCandidate("a" * 36, "", "", None, None, None, None, read_by_you=True)
        full = audio("a", 970.0, narrators=("Ray Porter",))
        [merged] = merge_editions([bare], [full])
        self.assertEqual((970.0, True), (merged.duration_minutes, merged.read_by_you))

    def test_narrator_breaks_a_runtime_tie_ahead_of_the_closer_runtime(self):
        other_reader = audio("a", 971.0, narrators=("Someone Else",))
        right_reader = audio("b", 978.0, narrators=("Ray Porter",))
        details = AudiobookDetails(narrators=("Ray Porter",))

        match, reason = match_audio_edition(
            [other_reader, right_reader], target_duration_minutes=970.9, details=details,
        )
        self.assertIs(right_reader, match)
        self.assertEqual(["narrator", "narrator_exact"], reason["decided_by"])

    def test_an_exact_narrator_list_breaks_a_tie_between_shared_narrators(self):
        # Project Hail Mary: both releases credit Ray Porter and share a
        # runtime and publisher; one also credits a second narrator.
        extra_credit = audio("a", 970.0, narrators=("Ray Porter", "David Sterling"))
        exact = audio("b", 970.0, narrators=("Ray Porter",))
        details = AudiobookDetails(narrators=("Ray Porter",), publisher="Audible Studios")

        match, reason = match_audio_edition([extra_credit, exact], target_duration_minutes=970.9, details=details)
        self.assertIs(exact, match)
        self.assertEqual(["narrator_exact"], reason["decided_by"])

    def test_a_shared_narrator_still_outranks_an_exact_list_with_a_different_publisher(self):
        details = AudiobookDetails(narrators=("Ray Porter",), publisher="Audible Studios")
        exact_elsewhere = audio("a", 970.0, narrators=("Ray Porter",), publisher="Penguin Audio")
        shared_here = audio("b", 972.0, narrators=("Ray Porter", "David Sterling"), publisher="Audible Studios")
        match, reason = match_audio_edition(
            [exact_elsewhere, shared_here], target_duration_minutes=970.9, details=details,
        )
        # Equal scores, so the closer runtime decides and nothing claims credit.
        self.assertIs(exact_elsewhere, match)
        self.assertNotIn("decided_by", reason)

    def test_the_edition_you_have_read_wins_a_runtime_tie(self):
        other = audio("a", 970.0)
        yours = replace(audio("b", 972.0), read_by_you=True)
        match, reason = match_audio_edition([other, yours], target_duration_minutes=970.9)
        self.assertIs(yours, match)
        self.assertEqual(["read_before"], reason["decided_by"])
        self.assertTrue(reason["read_edition"]["chosen"])

    def test_a_different_narrator_beats_the_edition_you_have_read(self):
        details = AudiobookDetails(narrators=("Ray Porter",))
        right_reader = audio("a", 970.0, narrators=("Ray Porter",))
        yours = replace(audio("b", 970.0, narrators=("Someone Else",)), read_by_you=True)
        match, reason = match_audio_edition([right_reader, yours], target_duration_minutes=970.9, details=details)
        self.assertIs(right_reader, match)
        self.assertEqual("outranked", reason["read_edition"]["problem"])
        self.assertFalse(reason["read_edition"]["narrator_check"])

    def test_says_why_the_edition_you_have_read_is_not_the_match(self):
        hardcover = EditionCandidate("c" * 36, "Book", "Hardcover", None, None, "English", "Pub", read_by_you=True)
        match, reason = match_audio_edition([audio("a", 970.0), hardcover], target_duration_minutes=970.9)
        self.assertEqual("a" * 36, match.book_id)
        self.assertEqual(("not_audio", "Hardcover"), (reason["read_edition"]["problem"], reason["read_edition"]["format"]))

        long_one = replace(audio("b", 1100.0), read_by_you=True)
        _, reason = match_audio_edition([audio("a", 970.0), long_one], target_duration_minutes=970.9)
        self.assertEqual(("runtime_mismatch", 129.1),
                         (reason["read_edition"]["problem"], reason["read_edition"]["delta_minutes"]))

    def test_the_edition_abs_is_tagged_with_wins_whatever_it_is(self):
        exact = replace(audio("a", 970.0), identifier="B01")
        tagged = EditionCandidate("c" * 36, "Book", "Hardcover", None, None, "Spanish", "Pub")
        match, reason = match_audio_edition(
            [exact, tagged], target_duration_minutes=970.9, identifiers=["B01"],
            details=AudiobookDetails(language="English"), tagged_id=tagged.book_id,
        )
        self.assertIs(tagged, match)
        self.assertEqual("tagged", reason["code"])

    def test_a_tag_outranks_the_edition_you_have_read(self):
        yours = replace(audio("a", 970.0), read_by_you=True)
        tagged = audio("b", 972.0)
        match, reason = match_audio_edition([yours, tagged], target_duration_minutes=970.9, tagged_id=tagged.book_id)
        self.assertIs(tagged, match)
        self.assertEqual("tagged_elsewhere", reason["read_edition"]["problem"])

        match, reason = match_audio_edition([yours, tagged], target_duration_minutes=970.9, tagged_id=yours.book_id)
        self.assertIs(yours, match)
        self.assertTrue(reason["read_edition"]["chosen"])

    def test_a_tag_for_an_edition_that_is_not_listed_changes_nothing(self):
        match, reason = match_audio_edition([audio("a", 970.0)], target_duration_minutes=970.9, tagged_id="d" * 36)
        self.assertEqual(("a" * 36, "runtime"), (match.book_id, reason["code"]))

    def test_a_matching_narrator_never_rescues_a_runtime_outside_tolerance(self):
        details = AudiobookDetails(narrators=("Ray Porter",))
        match, reason = match_audio_edition(
            [audio("a", 1100.0, narrators=("Ray Porter",))], target_duration_minutes=970.9, details=details,
        )
        self.assertIsNone(match)
        self.assertEqual("runtime_mismatch", reason["code"])

    def test_an_edition_in_another_language_is_never_chosen(self):
        details = AudiobookDetails(language="eng")
        spanish = audio("a", 970.0, language="Spanish")
        match, reason = match_audio_edition([spanish], target_duration_minutes=970.9, details=details)
        self.assertIsNone(match)
        self.assertEqual("language_mismatch", reason["code"])

        english = audio("b", 975.0, language="English")
        match, reason = match_audio_edition([spanish, english], target_duration_minutes=970.9, details=details)
        self.assertIs(english, match)
        self.assertEqual(1, reason["other_language_editions"])

    def test_checks_tolerate_loosely_named_publishers_and_say_when_they_cannot_tell(self):
        details = AudiobookDetails(narrators=("ray porter",), publisher="Penguin Audio", language="English")
        checks = edition_checks(audio("a", 1.0, publisher="Penguin Audio Ltd", narrators=("Ray Porter",)), details)
        self.assertEqual({"narrator": True, "narrator_exact": True, "publisher": True, "language": True}, checks)
        checks = edition_checks(audio("a", 1.0, publisher="Not specified", language=None), details)
        self.assertEqual({"narrator": None, "narrator_exact": None, "publisher": None, "language": None}, checks)


def audio(key, minutes, *, narrators=(), publisher="Pub", language="English"):
    return EditionCandidate(key * 36, "Book", "Audio", minutes, None, language, publisher, narrators)


if __name__ == "__main__":
    unittest.main()
