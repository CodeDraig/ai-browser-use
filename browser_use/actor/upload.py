from browser_use.dom.tree import EnhancedDOMTreeNode


class FileUploader:
	"""Resolve element sessions and populate file inputs through CDP."""

	def __init__(self, browser_session) -> None:
		self.browser_session = browser_session

	@property
	def logger(self):
		return self.browser_session.logger

	async def _get_session_id(self, element_node: EnhancedDOMTreeNode) -> str | None:
		if element_node.frame_id:
			try:
				for target_id, target in self.browser_session.session_manager.get_all_targets().items():
					if target.target_type == 'iframe' and element_node.frame_id in str(target_id):
						temp_session = await self.browser_session.get_or_create_cdp_session(target_id, focus=False)
						return temp_session.session_id
				self.logger.debug(f'Frame {element_node.frame_id} not found in targets, using main session')
			except Exception as exc:
				self.logger.debug(f'Error getting frame session: {exc}, using main session')

		cdp_session = await self.browser_session.get_or_create_cdp_session()
		return cdp_session.session_id

	async def upload(self, element_node: EnhancedDOMTreeNode, file_path: str) -> None:
		await self.browser_session.cdp_client.send.DOM.setFileInputFiles(
			params={'files': [file_path], 'backendNodeId': element_node.backend_node_id},
			session_id=await self._get_session_id(element_node),
		)
