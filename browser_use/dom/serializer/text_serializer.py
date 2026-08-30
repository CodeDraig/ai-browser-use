# @file purpose: Render simplified DOM trees as compact text for LLM consumption

from browser_use.dom.tree import EnhancedDOMTreeNode, NodeType, SimplifiedNode
from browser_use.dom.utils import cap_text_length


class DOMTextSerializer:
	"""Render an already simplified and indexed DOM tree."""

	@staticmethod
	def serialize_tree(node: SimplifiedNode | None, include_attributes: list[str], depth: int = 0) -> str:
		"""Serialize the optimized tree to string format."""
		if not node:
			return ''

		# Skip rendering excluded nodes, but process their children
		if hasattr(node, 'excluded_by_parent') and node.excluded_by_parent:
			formatted_text = []
			for child in node.children:
				child_text = DOMTextSerializer.serialize_tree(child, include_attributes, depth)
				if child_text:
					formatted_text.append(child_text)
			return '\n'.join(formatted_text)

		formatted_text = []
		depth_str = depth * '\t'
		next_depth = depth

		if node.original_node.node_type == NodeType.ELEMENT_NODE:
			# Skip displaying nodes marked as should_display=False
			if not node.should_display:
				for child in node.children:
					child_text = DOMTextSerializer.serialize_tree(child, include_attributes, depth)
					if child_text:
						formatted_text.append(child_text)
				return '\n'.join(formatted_text)

			# Special handling for SVG elements - show the tag but collapse children
			if node.original_node.tag_name.lower() == 'svg':
				shadow_prefix = ''
				if node.is_shadow_host:
					has_closed_shadow = any(
						child.original_node.node_type == NodeType.DOCUMENT_FRAGMENT_NODE
						and child.original_node.shadow_root_type
						and child.original_node.shadow_root_type.lower() == 'closed'
						for child in node.children
					)
					shadow_prefix = '|SHADOW(closed)|' if has_closed_shadow else '|SHADOW(open)|'

				line = f'{depth_str}{shadow_prefix}'
				# Add interactive marker if clickable
				if node.is_interactive:
					assert node.selector_index is not None
					new_prefix = '*' if node.is_new else ''
					line += f'{new_prefix}[{node.selector_index}]'
				line += '<svg'
				attributes_html_str = DOMTextSerializer._build_attributes_string(node.original_node, include_attributes, '')
				if attributes_html_str:
					line += f' {attributes_html_str}'
				line += ' /> <!-- SVG content collapsed -->'
				formatted_text.append(line)
				# Don't process children for SVG
				return '\n'.join(formatted_text)

			# Add element if clickable, scrollable, or iframe
			is_any_scrollable = node.original_node.is_actually_scrollable or node.original_node.is_scrollable
			should_show_scroll = node.original_node.should_show_scroll_info
			if (
				node.is_interactive
				or is_any_scrollable
				or node.original_node.tag_name.upper() == 'IFRAME'
				or node.original_node.tag_name.upper() == 'FRAME'
			):
				next_depth += 1

				# Build attributes string with compound component info
				text_content = ''
				attributes_html_str = DOMTextSerializer._build_attributes_string(
					node.original_node, include_attributes, text_content
				)

				# Add compound component information to attributes if present
				if node.original_node._compound_children:
					compound_info = []
					for child_info in node.original_node._compound_children:
						parts = []
						if child_info['name']:
							parts.append(f'name={child_info["name"]}')
						if child_info['role']:
							parts.append(f'role={child_info["role"]}')
						if child_info['valuemin'] is not None:
							parts.append(f'min={child_info["valuemin"]}')
						if child_info['valuemax'] is not None:
							parts.append(f'max={child_info["valuemax"]}')
						if child_info['valuenow'] is not None:
							parts.append(f'current={child_info["valuenow"]}')

						# Add select-specific information
						if 'options_count' in child_info and child_info['options_count'] is not None:
							parts.append(f'count={child_info["options_count"]}')
						if 'first_options' in child_info and child_info['first_options']:
							options_str = '|'.join(child_info['first_options'][:4])  # Limit to 4 options
							parts.append(f'options={options_str}')
						if 'format_hint' in child_info and child_info['format_hint']:
							parts.append(f'format={child_info["format_hint"]}')

						if parts:
							compound_info.append(f'({",".join(parts)})')

					if compound_info:
						compound_attr = f'compound_components={",".join(compound_info)}'
						if attributes_html_str:
							attributes_html_str += f' {compound_attr}'
						else:
							attributes_html_str = compound_attr

				# Build the line with shadow host indicator
				shadow_prefix = ''
				if node.is_shadow_host:
					# Check if any shadow children are closed
					has_closed_shadow = any(
						child.original_node.node_type == NodeType.DOCUMENT_FRAGMENT_NODE
						and child.original_node.shadow_root_type
						and child.original_node.shadow_root_type.lower() == 'closed'
						for child in node.children
					)
					shadow_prefix = '|SHADOW(closed)|' if has_closed_shadow else '|SHADOW(open)|'

				if should_show_scroll and not node.is_interactive:
					# Scrollable container but not clickable
					line = f'{depth_str}{shadow_prefix}|scroll element|<{node.original_node.tag_name}'
				elif node.is_interactive:
					assert node.selector_index is not None
					new_prefix = '*' if node.is_new else ''
					scroll_prefix = '|scroll element[' if should_show_scroll else '['
					line = f'{depth_str}{shadow_prefix}{new_prefix}{scroll_prefix}{node.selector_index}]<{node.original_node.tag_name}'
				elif node.original_node.tag_name.upper() == 'IFRAME':
					# Iframe element (not interactive)
					line = f'{depth_str}{shadow_prefix}|IFRAME|<{node.original_node.tag_name}'
				elif node.original_node.tag_name.upper() == 'FRAME':
					# Frame element (not interactive)
					line = f'{depth_str}{shadow_prefix}|FRAME|<{node.original_node.tag_name}'
				else:
					line = f'{depth_str}{shadow_prefix}<{node.original_node.tag_name}'

				if attributes_html_str:
					line += f' {attributes_html_str}'

				line += ' />'

				# Add scroll information only when we should show it
				if should_show_scroll:
					scroll_info_text = node.original_node.get_scroll_info_text()
					if scroll_info_text:
						line += f' ({scroll_info_text})'

				formatted_text.append(line)

		elif node.original_node.node_type == NodeType.DOCUMENT_FRAGMENT_NODE:
			# Shadow DOM representation - show clearly to LLM
			if node.original_node.shadow_root_type and node.original_node.shadow_root_type.lower() == 'closed':
				formatted_text.append(f'{depth_str}Closed Shadow')
			else:
				formatted_text.append(f'{depth_str}Open Shadow')

			next_depth += 1

			# Process shadow DOM children
			for child in node.children:
				child_text = DOMTextSerializer.serialize_tree(child, include_attributes, next_depth)
				if child_text:
					formatted_text.append(child_text)

			# Close shadow DOM indicator
			if node.children:  # Only show close if we had content
				formatted_text.append(f'{depth_str}Shadow End')

		elif node.original_node.node_type == NodeType.TEXT_NODE:
			# Include visible text that isn't fully covered by another element
			# painted on top of it (e.g. text underneath an open modal/dropdown).
			is_visible = node.original_node.snapshot_node and node.original_node.is_visible
			if (
				is_visible
				and not node.ignored_by_paint_order
				and node.original_node.node_value
				and node.original_node.node_value.strip()
				and len(node.original_node.node_value.strip()) > 1
			):
				clean_text = node.original_node.node_value.strip()
				formatted_text.append(f'{depth_str}{clean_text}')

		# Process children (for non-shadow elements)
		if node.original_node.node_type != NodeType.DOCUMENT_FRAGMENT_NODE:
			for child in node.children:
				child_text = DOMTextSerializer.serialize_tree(child, include_attributes, next_depth)
				if child_text:
					formatted_text.append(child_text)

			# Add hidden content hint for iframes
			if (
				node.original_node.node_type == NodeType.ELEMENT_NODE
				and node.original_node.tag_name
				and node.original_node.tag_name.upper() in ('IFRAME', 'FRAME')
			):
				if node.original_node.hidden_elements_info:
					# Show specific interactive elements with scroll distances
					hidden = node.original_node.hidden_elements_info
					hint_lines = [f'{depth_str}... ({len(hidden)} more elements below - scroll to reveal):']
					for elem in hidden:
						hint_lines.append(f'{depth_str}    <{elem["tag"]}> "{elem["text"]}" ~{elem["pages"]} pages down')
					formatted_text.extend(hint_lines)
				elif node.original_node.has_hidden_content:
					# Generic hint for non-interactive hidden content
					formatted_text.append(f'{depth_str}... (more content below viewport - scroll to reveal)')

		return '\n'.join(formatted_text)

	@staticmethod
	def _build_attributes_string(node: EnhancedDOMTreeNode, include_attributes: list[str], text: str) -> str:
		"""Build the attributes string for an element."""
		attributes_to_include = {}

		# Include HTML attributes
		if node.attributes:
			attributes_to_include.update(
				{
					key: str(value).strip()
					for key, value in node.attributes.items()
					if key in include_attributes and str(value).strip() != ''
				}
			)

		# Add format hints for date/time inputs to help LLMs use the correct format
		# NOTE: These formats are standardized by HTML5 specification (ISO 8601), NOT locale-dependent
		# The browser may DISPLAY dates in locale format (MM/DD/YYYY in US, DD/MM/YYYY in EU),
		# but the .value attribute and programmatic setting ALWAYS uses these ISO formats:
		# - date: YYYY-MM-DD (e.g., "2024-03-15")
		# - time: HH:MM or HH:MM:SS (24-hour, e.g., "14:30")
		# - datetime-local: YYYY-MM-DDTHH:MM (e.g., "2024-03-15T14:30")
		# Reference: https://developer.mozilla.org/en-US/docs/Web/HTML/Element/input/date
		if node.tag_name and node.tag_name.lower() == 'input' and node.attributes:
			input_type = node.attributes.get('type', '').lower()

			# For HTML5 date/time inputs, add a highly visible "format" attribute
			# This makes it IMPOSSIBLE for the model to miss the required format
			if input_type in ['date', 'time', 'datetime-local', 'month', 'week']:
				format_map = {
					'date': 'YYYY-MM-DD',
					'time': 'HH:MM',
					'datetime-local': 'YYYY-MM-DDTHH:MM',
					'month': 'YYYY-MM',
					'week': 'YYYY-W##',
				}
				# Add format as a special attribute that appears prominently
				# This appears BEFORE placeholder in the serialized output
				attributes_to_include['format'] = format_map[input_type]

			# Only add placeholder if it doesn't already exist
			if 'placeholder' in include_attributes and 'placeholder' not in attributes_to_include:
				# Native HTML5 date/time inputs - ISO format required
				if input_type == 'date':
					attributes_to_include['placeholder'] = 'YYYY-MM-DD'
				elif input_type == 'time':
					attributes_to_include['placeholder'] = 'HH:MM'
				elif input_type == 'datetime-local':
					attributes_to_include['placeholder'] = 'YYYY-MM-DDTHH:MM'
				elif input_type == 'month':
					attributes_to_include['placeholder'] = 'YYYY-MM'
				elif input_type == 'week':
					attributes_to_include['placeholder'] = 'YYYY-W##'
				# Tel - suggest format if no pattern attribute
				elif input_type == 'tel' and 'pattern' not in attributes_to_include:
					attributes_to_include['placeholder'] = '123-456-7890'
				# jQuery/Bootstrap/AngularJS datepickers (text inputs with datepicker classes/attributes)
				elif input_type in {'text', ''}:
					class_attr = node.attributes.get('class', '').lower()

					# Check for AngularJS UI Bootstrap datepicker (uib-datepicker-popup attribute)
					# This takes precedence as it's the most specific indicator
					if 'uib-datepicker-popup' in node.attributes:
						# Extract format from uib-datepicker-popup="MM/dd/yyyy"
						date_format = node.attributes.get('uib-datepicker-popup', '')
						if date_format:
							# Use 'expected_format' for clarity - this is the required input format
							attributes_to_include['expected_format'] = date_format
							# Also keep format for consistency with HTML5 date inputs
							attributes_to_include['format'] = date_format
					# Detect jQuery/Bootstrap datepickers by class names
					elif any(indicator in class_attr for indicator in ['datepicker', 'datetimepicker', 'daterangepicker']):
						# Try to get format from data-date-format attribute
						date_format = node.attributes.get('data-date-format', '')
						if date_format:
							attributes_to_include['placeholder'] = date_format
							attributes_to_include['format'] = date_format  # Also add format for jQuery datepickers
						else:
							# Default to common US format for jQuery datepickers
							attributes_to_include['placeholder'] = 'mm/dd/yyyy'
							attributes_to_include['format'] = 'mm/dd/yyyy'
					# Also detect by data-* attributes
					elif any(attr in node.attributes for attr in ['data-datepicker']):
						date_format = node.attributes.get('data-date-format', '')
						if date_format:
							attributes_to_include['placeholder'] = date_format
							attributes_to_include['format'] = date_format
						else:
							attributes_to_include['placeholder'] = 'mm/dd/yyyy'
							attributes_to_include['format'] = 'mm/dd/yyyy'

		# Never include values from password fields - they contain secrets that must not
		# leak into DOM snapshots sent to the LLM, where prompt injection could exfiltrate them.
		is_password_field = (
			node.tag_name
			and node.tag_name.lower() == 'input'
			and node.attributes
			and node.attributes.get('type', '').lower() == 'password'
		)

		# Include accessibility properties
		if node.ax_node and node.ax_node.properties:
			# Properties that carry field values - must be excluded for password fields
			value_properties = {'value', 'valuetext'}
			for prop in node.ax_node.properties:
				try:
					if prop.name in include_attributes and prop.value is not None:
						if is_password_field and prop.name in value_properties:
							continue
						# Convert boolean to lowercase string, keep others as-is
						if isinstance(prop.value, bool):
							attributes_to_include[prop.name] = str(prop.value).lower()
						else:
							prop_value_str = str(prop.value).strip()
							if prop_value_str:
								attributes_to_include[prop.name] = prop_value_str
				except (AttributeError, ValueError):
					continue

		# Special handling for form elements - ensure current value is shown
		# For text inputs, textareas, and selects, prioritize showing the current value from AX tree
		if node.tag_name and node.tag_name.lower() in ['input', 'textarea', 'select']:
			if is_password_field:
				attributes_to_include.pop('value', None)
			# ALWAYS check AX tree - it reflects actual typed value, DOM attribute may not update
			elif node.ax_node and node.ax_node.properties:
				for prop in node.ax_node.properties:
					# Try valuetext first (human-readable display value)
					if prop.name == 'valuetext' and prop.value:
						value_str = str(prop.value).strip()
						if value_str:
							attributes_to_include['value'] = value_str
							break
					# Also try 'value' property directly
					elif prop.name == 'value' and prop.value:
						value_str = str(prop.value).strip()
						if value_str:
							attributes_to_include['value'] = value_str
							break

		if not attributes_to_include:
			return ''

		# Remove duplicate values
		ordered_keys = [key for key in include_attributes if key in attributes_to_include]

		if len(ordered_keys) > 1:
			keys_to_remove = set()
			seen_values = {}

			# Attributes that should never be removed as duplicates (they serve distinct purposes)
			protected_attrs = {'format', 'expected_format', 'placeholder', 'value', 'aria-label', 'title'}

			for key in ordered_keys:
				value = attributes_to_include[key]
				if len(value) > 5:
					if value in seen_values and key not in protected_attrs:
						keys_to_remove.add(key)
					else:
						seen_values[value] = key

			for key in keys_to_remove:
				del attributes_to_include[key]

		# Remove attributes that duplicate accessibility data
		role = node.ax_node.role if node.ax_node else None
		if role and node.node_name == role:
			attributes_to_include.pop('role', None)

		# Remove type attribute if it matches the tag name (e.g. <button type="button">)
		if 'type' in attributes_to_include and attributes_to_include['type'].lower() == node.node_name.lower():
			del attributes_to_include['type']

		# Remove invalid attribute if it's false (only show when true)
		if 'invalid' in attributes_to_include and attributes_to_include['invalid'].lower() == 'false':
			del attributes_to_include['invalid']

		boolean_attrs = {'required'}
		for attr in boolean_attrs:
			if attr in attributes_to_include and attributes_to_include[attr].lower() in {'false', '0', 'no'}:
				del attributes_to_include[attr]

		# Remove aria-expanded if we have expanded (prefer AX tree over HTML attribute)
		if 'expanded' in attributes_to_include and 'aria-expanded' in attributes_to_include:
			del attributes_to_include['aria-expanded']

		attrs_to_remove_if_text_matches = ['aria-label', 'placeholder', 'title']
		for attr in attrs_to_remove_if_text_matches:
			if attributes_to_include.get(attr) and attributes_to_include.get(attr, '').strip().lower() == text.strip().lower():
				del attributes_to_include[attr]

		if attributes_to_include:
			# Format attributes, wrapping empty values in quotes for clarity
			formatted_attrs = []
			for key, value in attributes_to_include.items():
				capped_value = cap_text_length(value, 100)
				# Show empty values as key='' instead of key=
				if not capped_value:
					formatted_attrs.append(f"{key}=''")
				else:
					formatted_attrs.append(f'{key}={capped_value}')
			return ' '.join(formatted_attrs)

		return ''
