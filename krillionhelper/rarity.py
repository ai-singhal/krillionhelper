"""Word-frequency rarity: how often an answer turns up in written text (books, web, subtitles,
Wikipedia), via the wordfreq library. Krillion scores many prompts the same way, from a
"familiarity database of how often a word or name actually turns up" (krillion.io/faq).
"""

try:
    from wordfreq import zipf_frequency
except ImportError:  # optional dependency
    zipf_frequency = None


def frequencyRarity(answer):
    """0 (everyone writes it, like "apple") to 4 (almost never written), or None if unavailable.

    Zipf scale: ~5 for "apple", ~2 for "kumquat", 0 for words wordfreq has never seen.
    wordfreq estimates a phrase from its words, which overstates how common a multi-word name
    is, so each extra word counts as one Zipf step rarer.
    """
    if zipf_frequency is None or not answer:
        return None
    zipf = zipf_frequency(answer, "en")
    zipf -= 1.0 * (len(answer.split()) - 1)
    return max(0.0, min(4.0, (5.5 - zipf) * 0.9))
