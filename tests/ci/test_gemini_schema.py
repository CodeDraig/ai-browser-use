from browser_use.llm.gemini_schema import normalize_gemini_schema


def test_gemini_schema_normalization_resolves_refs_and_preserves_title_property():
	schema = {
		'$defs': {'Entry': {'type': 'object', 'properties': {'title': {'type': 'string'}}, 'required': ['title']}},
		'type': 'object',
		'title': 'Response',
		'properties': {'entry': {'$ref': '#/$defs/Entry'}, 'empty': {'type': 'object', 'properties': {}}},
		'additionalProperties': False,
	}

	normalized = normalize_gemini_schema(schema)

	assert normalized == {
		'type': 'object',
		'properties': {
			'entry': {'type': 'object', 'properties': {'title': {'type': 'string'}}, 'required': ['title']},
			'empty': {'type': 'object', 'properties': {'_placeholder': {'type': 'string'}}},
		},
	}
	assert '$defs' in schema
