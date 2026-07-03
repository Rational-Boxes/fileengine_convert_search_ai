# Backend service providing format conversion, search, and vector backed search and chat

The Python and FastAPI microservice that integrates with the FileEngine gRPC API,
with authentication against LDAP. The service provides a plugin based fie conversion
that saves alternate renditions in the hidden children of fies in FileEngine. Conversion
also, if at all possible, converts the document content to Markdown, saves the content to
Postgres to drive search, and chunk and vectorize for LLM based search and chat. The retrival
must use the permission check of the FileEngine to gate information that makes it into 
search results and RAG for interactive chat. The extracted Markdown data can also be used for
advanced AI based information extraction, but that is its own microservice outside this scope.

## Plugin based extraction

Framework to detect MIME type, file an appropriate installed conversion plugin, then
generate preview images and PDFs that can be loaded in the front-end application. Office
type documents will be processed through Open/LibreOffice, images processd into thumbnauils
and previews via Imagemagick, and web-optimized video preview using FFMPEG, and any other
formats with an installed plugin.

## Storage for search and chat

The textual content of files is stored in a Postgres table both as the full text and chunked
and vectorized. However, every piece of information must be related to the source document in
FileEngine, and using the permission check make sure the user has read access to any given piece
of information. The permission can be cached, but limit to five minuets before refreshing
the access.

## Search back-end

Full text search against the text converted to Markdown format and stored in the database
is available for full-text search with fuzzy match features. This must respect permissions
for any matched result. Naturally, the results must reference the file ID in FileEngine.

## Text request

API operation to request the extracted text, used for integration with AI based document
analysis services.

## Chat-with-documents

When an appropriate LLM API configured, the chunked document content respecting file
permissions is available via a RAG based interactive chat served over WebSockets. A
frontend connects and where a document reference is returned in replies the link
to the document is presented. THe front-end provides a conversation specific system prompt
to seed the conversation. This way a system user can conduct a chat-with-documents session
limited to only content they have read access to.

The full feature set — the WebSocket protocol, permission‑scoped retrieval, agentic
tools (web search/fetch, folder browse, report saving), citations, conversations, and
the planned expansion — is documented in **[CHAT_WITH_AI.md](./CHAT_WITH_AI.md)**.