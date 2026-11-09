"""O6 algorithm (app/textlayer_check.py): the layer-vs-render comparison on synthetic word lists, no PDF and no OCR."""
import time
from dataclasses import replace

import numpy as np
import pytest
from PIL import Image

from app.models import OcrLine, OcrPage, OcrWord
from app.textlayer_check import (TooManyWordsError, compare_page, differing_chars, make_ink_fn, normalize)
from app.textlayer_check_config import load_text_layer_check_config

CFG = load_text_layer_check_config()
W, H = 1240, 1754
BLANK = lambda box, exclude=(): 0.0   # noqa: E731  nothing is drawn there
INKED = lambda box, exclude=(): 0.5   # noqa: E731  something is drawn there


def word(text, x, y, w=0.06, h=0.012, conf=1.0):
    return (text, (x, y, w, h), conf)


def page(words, engine="test"):
    ow = [OcrWord(text=t, bbox=b, ocrConfidence=c, lineIndex=0) for t, b, c in words]
    lines = [OcrLine(text=" ".join(t for t, _, _ in words), bbox=(0, 0, 1, 1), ocrConfidence=1.0,
                     wordIndices=list(range(len(ow))))] if ow else []
    return OcrPage(docId="d", page=1, widthPx=W, heightPx=H, renderDpi=150, engine=engine, engineVersion="1", words=ow, lines=lines)


def row(texts, y=0.3, start=0.1, step=0.09):
    return [word(t, start + i * step, y) for i, t in enumerate(texts)]


def compare(layer, ocr, ink=BLANK, cfg=CFG, reread=None):
    return compare_page(page(layer), page(ocr), ink, cfg, reread)


SENTENCE = ["Applicant", "Name", "John", "Smith", "Account", "Number", "12345678", "Closing", "Balance", "2,450.00"]


# ------------------------------------------------------------------ agreement

def test_identical_readings_report_nothing():
    r = compare(row(SENTENCE), row(SENTENCE))
    assert r.mismatches == [] and r.agreement == 1.0 and not r.page_level and not r.not_comparable


def test_ocr_confusions_and_case_are_not_differences():
    r = compare(row(["INVOICE", "Total", "1O5.00", "Slip"]), row(["invoice", "T0tal", "105.00", "5lip"]))
    assert r.mismatches == []


def test_ocr_merging_two_layer_words_into_one_is_a_match():
    layer = [word("USD", 0.10, 0.4, 0.05), word("21,500.00", 0.16, 0.4, 0.10)]
    ocr = [word("USD21,500.00", 0.10, 0.4, 0.16)]
    assert compare(layer, ocr).mismatches == []


def test_ocr_splitting_one_layer_word_into_two_is_a_match():
    layer = [word("USD21,500.00", 0.10, 0.4, 0.16)]
    ocr = [word("USD", 0.10, 0.4, 0.05), word("21,500.00", 0.16, 0.4, 0.10)]
    assert compare(layer, ocr).mismatches == []


def test_two_character_word_merged_into_its_neighbour_is_visible_not_hidden():
    # "16 Feb 2026" read by OCR as "16Feb 2026": the layer word "16" is covered, not hidden (it used to be called hidden)
    layer = [word("16", 0.098, 0.521, 0.015, 0.015), word("Feb", 0.117, 0.521, 0.024, 0.015), word("2026", 0.145, 0.521, 0.031, 0.015)]
    ocr = [word("16Feb", 0.099, 0.525, 0.042, 0.008), word("2026", 0.146, 0.525, 0.029, 0.008)]
    r = compare(layer, ocr, ink=BLANK)
    assert r.mismatches == []


def test_a_layer_word_that_ocr_covers_is_never_reported_as_hidden():
    # OCR words lie over it but are explained by other layer words: counted as visible-but-unexplained, not "nothing visible"
    layer = [word("alpha", 0.10, 0.4, 0.10, 0.02), word("zz", 0.115, 0.4, 0.03, 0.02)]
    ocr = [word("alpha", 0.10, 0.405, 0.10, 0.012)]
    r = compare(layer, ocr, ink=BLANK)
    assert all(m.kind != "layer_only" for m in r.mismatches)


def test_each_differing_cluster_is_verified_on_its_own():
    values = ["8,988.49", "8,936.28", "8,906.93", "8,881.23"]
    column = [word(v, 0.6, 0.3 + i * 0.02, 0.054, 0.015) for i, v in enumerate(values)]
    misread = [word(v, 0.6, 0.3 + i * 0.02, 0.054, 0.015) for i, v in enumerate(["2,988.49", "8,936.28", "8,906.98", "8,881.23"])]
    sharper_reading = {0: "8,988.49", 2: "9,999.99"}   # row 0: agrees with the layer (the first reading was a misread); row 2: does not

    def sharper(box):
        return sharper_reading[round((box[1] - 0.3) / 0.02)]

    r = compare(column, misread, ink=INKED, reread=sharper)
    assert [m.layer_text for m in r.mismatches] == ["8,906.93"] and r.reread_dropped == 1   # one stray digit does not keep the column alive
    assert r.mismatches[0].reread_text == "9,999.99"


def test_short_and_punctuation_words_are_ignored():
    r = compare([word("-", 0.1, 0.1), word("a", 0.2, 0.1), word("•", 0.3, 0.1)], [])
    assert r.mismatches == [] and r.ignored_layer_words == 3


# ------------------------------------------------------------------ hidden (layer only)

def test_layer_word_with_blank_area_is_hidden():
    r = compare(row(SENTENCE) + [word("SECRET", 0.5, 0.6, 0.1)], row(SENTENCE))
    assert [(m.kind, m.layer_text, m.reason) for m in r.mismatches] == [("layer_only", "SECRET", "nothing visible")]


def test_layer_word_over_visible_ink_is_not_called_hidden():
    # OCR simply failed to read it: ink is there, so it is counted but not reported
    r = compare(row(SENTENCE) + [word("faint", 0.5, 0.6, 0.1)], row(SENTENCE), ink=INKED)
    assert r.mismatches == [] and r.unread_ink == 1


def test_ink_that_cannot_be_judged_is_not_reported():
    r = compare(row(SENTENCE) + [word("SECRET", 0.5, 0.6, 0.1)], row(SENTENCE), ink=lambda b, e=(): None)
    assert r.mismatches == [] and r.unread_ink == 1


def test_neighbouring_hidden_words_form_one_signal():
    layer = row(SENTENCE) + [word("USD", 0.5, 0.6, 0.04), word("56,500.00", 0.55, 0.6, 0.09)]
    r = compare(layer, row(SENTENCE))
    assert [m.layer_text for m in r.mismatches] == ["USD 56,500.00"] and r.mismatches[0].layer_words == 2


def test_hidden_words_on_different_lines_are_separate_signals():
    layer = row(SENTENCE) + [word("ALPHA", 0.5, 0.6, 0.06), word("BRAVO", 0.5, 0.7, 0.06)]
    assert [m.layer_text for m in compare(layer, row(SENTENCE)).mismatches] == ["ALPHA", "BRAVO"]


def test_stacked_hidden_lines_in_one_column_form_one_signal():
    layer = row(SENTENCE) + [word("FIRST", 0.5, 0.60, 0.08), word("SECOND", 0.5, 0.616, 0.08), word("THIRD", 0.5, 0.632, 0.08)]
    r = compare(layer, row(SENTENCE))
    assert [m.layer_text for m in r.mismatches] == ["FIRST SECOND THIRD"]   # delivered reading order, not re-sorted


def test_vertical_text_hidden_word_by_word_is_one_signal_in_reading_order():
    tall = lambda t, y, h: word(t, 0.85, y, 0.012, h)   # noqa: E731  words of a rotated page are tall and narrow
    layer = row(SENTENCE) + [tall("USD", 0.50, 0.03), tall("99,999.00", 0.54, 0.09)]
    assert [m.layer_text for m in compare(layer, row(SENTENCE)).mismatches] == ["USD 99,999.00"]


def test_tiny_difference_is_ignored():
    cfg = replace(CFG, minGroupChars=4)
    assert compare(row(SENTENCE) + [word("abc", 0.5, 0.6, 0.03)], row(SENTENCE), cfg=cfg).mismatches == []
    assert len(compare(row(SENTENCE) + [word("abcd", 0.5, 0.6, 0.03)], row(SENTENCE), cfg=cfg).mismatches) == 1


def test_unreadably_small_unread_word_is_reported_even_over_ink():
    tiny = word("HIDDEN", 0.5, 0.6, 0.05, h=3 / H)  # 3 px high: no one can read it
    r = compare(row(SENTENCE) + [tiny], row(SENTENCE), ink=INKED)
    assert [(m.kind, m.reason) for m in r.mismatches] == [("layer_only", "text too small to read")]


def test_words_touching_the_page_edge_are_not_judged():
    clipped = [word("Balance", 0.95, 0.5, 0.0495), word("Edge", 0.2, 0.9995, 0.06, 0.0004)]
    r = compare(row(SENTENCE) + clipped, row(SENTENCE))
    assert r.mismatches == []


# ------------------------------------------------------------------ differs (stale or overlaid text)

def test_different_visible_reading_at_the_same_place_is_reported():
    layer = row(SENTENCE[:-1]) + [word("21,500.00", 0.5, 0.6, 0.1)]
    ocr = row(SENTENCE[:-1]) + [word("51,500.00", 0.5, 0.6, 0.1)]
    r = compare(layer, ocr, ink=INKED)
    assert [(m.kind, m.layer_text, m.render_text) for m in r.mismatches] == [("differs", "21,500.00", "51,500.00")]
    assert r.unverified == 1 and differing_chars("21,500.00", "51,500.00") == 1


def test_second_reading_that_agrees_with_the_layer_explains_the_difference_away():
    layer = row(SENTENCE[:-1]) + [word("1,796.60", 0.5, 0.6, 0.1)]
    ocr = row(SENTENCE[:-1]) + [word("4,796.60", 0.5, 0.6, 0.1)]
    r = compare(layer, ocr, ink=INKED, reread=lambda box: "1,796.60")
    assert r.mismatches == [] and r.reread_dropped == 1


def test_second_reading_that_still_disagrees_keeps_the_signal_and_records_the_reading():
    layer = row(SENTENCE[:-1]) + [word("21,500.00", 0.5, 0.6, 0.1)]
    ocr = row(SENTENCE[:-1]) + [word("51,500.00", 0.5, 0.6, 0.1)]
    r = compare(layer, ocr, ink=INKED, reread=lambda box: "51,500.00")
    assert [m.reread_text for m in r.mismatches] == ["51,500.00"] and r.reread_dropped == 0


def test_digits_need_a_near_exact_reading_but_letters_tolerate_small_errors():
    assert compare(row(["Statement", "Closing"]), row(["Staternent", "Closing"]), ink=INKED).mismatches == []   # rn for m
    r = compare(row(["Closing", "2,450.00"]), row(["Closing", "2,460.00"]), ink=INKED)
    assert [m.kind for m in r.mismatches] == ["differs"]


def test_ocr_box_inflated_over_a_neighbouring_label_is_not_a_difference():
    # the highlighted-value control: a tall OCR box for the value reaches up over the label that OCR did not read
    layer = row(SENTENCE) + [word("Application", 0.83, 0.068, 0.06, 0.013), word("No.", 0.893, 0.068, 0.019, 0.013)]
    ocr = row(SENTENCE) + [word("244884", 0.856, 0.072, 0.055, 0.025)]
    r = compare(layer, ocr, ink=INKED)
    assert r.mismatches == []


def test_low_confidence_ocr_words_are_not_trusted_as_visible_text():
    layer = row(SENTENCE[:-1]) + [word("2,450.00", 0.5, 0.6, 0.1)]
    ocr = row(SENTENCE[:-1]) + [word("9,999.99", 0.5, 0.6, 0.1, conf=0.2)]
    assert compare(layer, ocr, ink=INKED).mismatches == []


# ------------------------------------------------------------------ visible but absent from the layer

def test_render_only_words_are_off_by_default_and_reported_when_enabled():
    layer, ocr = row(SENTENCE), row(SENTENCE) + [word("STAMPED", 0.5, 0.8, 0.1)]
    assert compare(layer, ocr).mismatches == []
    r = compare(layer, ocr, cfg=replace(CFG, flagRenderOnly=True))
    assert [(m.kind, m.render_text) for m in r.mismatches] == [("render_only", "STAMPED")]


# ------------------------------------------------------------------ whole-page situations

def test_ocr_that_read_plenty_but_disagrees_is_a_page_level_finding():
    layer = row(["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"])
    ocr = row(["one", "two", "three", "four", "five", "six", "seven", "eight"])
    r = compare(layer, ocr, ink=INKED)
    assert r.page_level and not r.not_comparable and r.agreement == 0.0 and r.mismatches == []


def test_ocr_that_read_almost_nothing_means_not_comparable_not_a_finding():
    layer = row(["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"])
    r = compare(layer, row(["alpha"]), ink=INKED)
    assert r.not_comparable and not r.page_level and r.mismatches == []


def test_blank_words_are_still_found_on_a_page_ocr_could_not_read():
    layer = row(["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"], step=0.07)
    r = compare(layer, [], ink=BLANK)
    assert r.not_comparable and [m.kind for m in r.mismatches] == ["layer_only"]


def test_empty_layer_compares_to_nothing():
    r = compare([], row(SENTENCE))
    assert r.layer_words == 0 and r.mismatches == [] and not r.page_level and not r.not_comparable


# ------------------------------------------------------------------ limits and hostile input

def test_signals_per_page_are_capped_and_the_overflow_is_counted():
    cfg = replace(CFG, maxSignalsPerPage=3)
    layer = [word(f"hidden{n}", 0.05 + (n % 4) * 0.22, 0.1 + (n // 4) * 0.05, 0.09) for n in range(20)]  # 20 separate spots
    r = compare(layer, row(SENTENCE, y=0.95), cfg=cfg)
    assert len(r.mismatches) == 3 and r.dropped >= 1


def test_too_many_words_are_refused():
    cfg = replace(CFG, maxWordsPerPage=50)
    with pytest.raises(TooManyWordsError):
        compare(row(["word"] * 60, step=0.001), [], cfg=cfg)


def test_non_finite_and_degenerate_boxes_are_ignored_not_fatal():
    bad = [("nan", (float("nan"), 0.1, 0.1, 0.1), 1.0), ("inf", (0.1, 0.1, float("inf"), 0.1), 1.0),
           ("neg", (0.1, 0.1, -0.2, 0.1), 1.0), ("zero", (0.1, 0.1, 0.0, 0.1), 1.0)]
    r = compare(row(SENTENCE) + bad, row(SENTENCE))
    assert r.mismatches == [] and r.ignored_layer_words == 4


def test_dense_pages_stay_fast():
    n = 12000
    layer = [word(f"w{i:05d}", (i % 100) * 0.0098 + 0.005, (i // 100) * 0.0075 + 0.01, 0.009, 0.006) for i in range(n)]
    start = time.monotonic()
    r = compare_page(page(layer), page(layer), INKED, replace(CFG, maxWordsPerPage=20000, edgeMarginFrac=0.0, minBoxHeightPx=1))
    assert time.monotonic() - start < 15 and r.mismatches == []


def test_normalize_and_differing_chars():
    assert normalize("  Ça-va!  ") == normalize("ça va") and normalize("--") == ""
    assert differing_chars("48,562.75", "49,562.75") == 1 and differing_chars("abc", "") == 3 and differing_chars("", "") == 0


# ------------------------------------------------------------------ pixel test

def test_ink_function_tells_blank_from_drawn_and_excludes_read_words():
    arr = np.full((200, 400), 255, dtype=np.uint8)
    arr[100:120, 50:70] = 0             # dark glyphs at the start of the layer word's box: a neighbour OCR read
    ink = make_ink_fn(Image.fromarray(arr), CFG)
    box = (0.125, 0.5, 0.25, 0.1)       # pixels x 50..150, y 100..120
    assert ink((0.02, 0.1, 0.1, 0.1), ()) == 0.0                                # empty paper
    assert ink(box, ()) > CFG.inkMinFraction                                    # the glyphs make the area look drawn
    assert ink(box, [(0.12, 0.49, 0.06, 0.12)]) == 0.0                          # ... until the read word's pixels are left out
    assert ink(box, [(0.0, 0.0, 1.0, 1.0)]) is None                             # everything excluded: cannot tell
    assert ink(box, [(0.12, 0.49, 0.13, 0.12)]) is None                         # half excluded: the rest is not representative
