import unittest

from journal import parse_journal_page, progress_dates

# A representative excerpt of StoryGraph's /journal?book_id=<id> markup,
# captured live: one status-only entry ("Started reading") and one
# percent-bearing entry, matching the real structure of that page.
JOURNAL_HTML = """
<span class="journal-entry-panes">
  <div class="mb-3 grid grid-cols-4 md:grid-cols-6">
    <div class="col-span-4 md:col-span-4 ml-2 md:ml-2">
      <div class="mb-4">
        <p class="font-semibold text-xs md:text-sm">6 January 2026
        <a class=" pb-4 text-xs md:text-sm standard-link float-right" href="/journal_entries/ea10c7b4-e467-4186-928b-35f847d9cad4/edit?return_to=%2Fjournal%3Fbook_id%3D51a6a008-4a40-4c72-90e4-f4e73e183324">Edit</a></p>
      </div>
      <div class="mt-2 mb-4">
       <span title="Starting reading this book" class="inline-flex items-center px-2 py-1 rounded text-xs font-semibold bg-blueGrey-300 dark:bg-blueGrey-600 text-white dark:text-darkestGrey">
          Started reading
        </span>
      </div>
      <hr class="text-darkGrey dark:text-darkerGrey mt-8 mb-4 clear-both">
    </div>
  </div>
  <div class="mb-3 grid grid-cols-4 md:grid-cols-6">
    <div class="col-span-4 md:col-span-4 ml-2 md:ml-2">
      <div class="mb-4">
        <p class="font-semibold text-xs md:text-sm">7 January 2026
        <a class=" pb-4 text-xs md:text-sm standard-link float-right" href="/journal_entries/5a159496-b625-493b-96b2-81b3dffe9744/edit?return_to=%2Fjournal%3Fbook_id%3D51a6a008-4a40-4c72-90e4-f4e73e183324">Edit</a></p>
      </div>
      <div class="mt-2 mb-4">
        <div class="relative">
          <div class="overflow-hidden h-4 mt-3 mb-3 w-7/12 float-left text-xs flex rounded-md bg-darkGrey dark:bg-darkerGrey">
            <div style="width:13.000%" class="shadow-none flex flex-col whitespace-nowrap text-white dark:text-darkestGrey justify-center bg-teal-500 dark:bg-teal-400"></div>
          </div>
          <div class="inline text-teal-500 dark:text-teal-400 text-xs float-left ml-1 mt-3 font-semibold">13%</div>
        </div>
        <p class="clear-both text-xs text-darkerGrey dark:text-lightGrey mb-2 font-medium">
            11 pages read
            <span class="font-normal">(49 pages out of 369)</span>
        </p>
      </div>
      <hr class="text-darkGrey dark:text-darkerGrey mt-8 mb-4 clear-both">
    </div>
  </div>
</span>
"""


class JournalParsingTests(unittest.TestCase):
    def test_parses_a_status_only_entry_with_no_percent(self):
        entries = {e.entry_id: e for e in parse_journal_page(JOURNAL_HTML)}
        entry = entries["ea10c7b4-e467-4186-928b-35f847d9cad4"]
        self.assertEqual("2026-01-06", entry.date)
        self.assertIsNone(entry.percent)

    def test_parses_a_percent_bearing_entry(self):
        entries = {e.entry_id: e for e in parse_journal_page(JOURNAL_HTML)}
        entry = entries["5a159496-b625-493b-96b2-81b3dffe9744"]
        self.assertEqual("2026-01-07", entry.date)
        self.assertEqual(13.0, entry.percent)

    def test_deduplicates_by_entry_id(self):
        duplicated = JOURNAL_HTML + JOURNAL_HTML
        entries = parse_journal_page(duplicated)
        self.assertEqual(2, len(entries))


class ProgressDatesTests(unittest.TestCase):
    def test_ignores_status_only_entries(self):
        # The "Started reading" entry on 6 January carries no percent. Counting
        # it would let ensure_status()'s own status write block the import of
        # that same day's listening.
        self.assertEqual({"2026-01-07"}, progress_dates(parse_journal_page(JOURNAL_HTML)))

    def test_is_empty_when_nothing_has_a_percent(self):
        self.assertEqual(set(), progress_dates([]))


if __name__ == "__main__":
    unittest.main()
