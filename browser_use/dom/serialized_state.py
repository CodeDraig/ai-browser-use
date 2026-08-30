from dataclasses import dataclass

from browser_use.dom.tree import DOMSelectorMap, SimplifiedNode

DEFAULT_INCLUDE_ATTRIBUTES = [
	'title',
	'type',
	'checked',
	'id',
	'name',
	'role',
	'value',
	'placeholder',
	'data-date-format',
	'alt',
	'aria-label',
	'aria-expanded',
	'data-state',
	'aria-checked',
	'aria-valuemin',
	'aria-valuemax',
	'aria-valuenow',
	'aria-placeholder',
	'pattern',
	'min',
	'max',
	'minlength',
	'maxlength',
	'step',
	'accept',
	'multiple',
	'inputmode',
	'autocomplete',
	'aria-autocomplete',
	'list',
	'data-mask',
	'data-inputmask',
	'data-datepicker',
	'format',
	'expected_format',
	'contenteditable',
	'pseudo',
	'selected',
	'expanded',
	'pressed',
	'disabled',
	'invalid',
	'valuemin',
	'valuemax',
	'valuenow',
	'keyshortcuts',
	'haspopup',
	'multiselectable',
	'required',
	'valuetext',
	'level',
	'busy',
	'live',
	'ax_name',
]


@dataclass(slots=True)
class MarkdownChunk:
	"""A structure-aware chunk of markdown content."""

	content: str
	chunk_index: int
	total_chunks: int
	char_offset_start: int
	char_offset_end: int
	overlap_prefix: str
	has_more: bool


@dataclass
class SerializedDOMState:
	_root: SimplifiedNode | None
	"""Not meant to be used directly, use `llm_representation` instead."""

	selector_map: DOMSelectorMap

	def llm_representation(self, include_attributes: list[str] | None = None) -> str:
		from browser_use.dom.serializer.text_serializer import DOMTextSerializer

		if not self._root:
			return 'Empty DOM tree (you might have to wait for the page to load)'
		return DOMTextSerializer.serialize_tree(self._root, include_attributes or DEFAULT_INCLUDE_ATTRIBUTES)

	def eval_representation(self, include_attributes: list[str] | None = None) -> str:
		from browser_use.dom.serializer.eval_serializer import DOMEvalSerializer

		if not self._root:
			return 'Empty DOM tree (you might have to wait for the page to load)'
		return DOMEvalSerializer.serialize_tree(self._root, include_attributes or DEFAULT_INCLUDE_ATTRIBUTES)
