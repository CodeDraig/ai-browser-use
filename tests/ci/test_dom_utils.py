from browser_use.dom.utils import sanitize_surrogates


def test_sanitize_surrogates_removes_unpaired_values_and_preserves_unicode():
	assert sanitize_surrogates('before\ud800after ✓') == 'beforeafter ✓'
