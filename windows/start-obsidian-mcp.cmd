@echo off
rem ---------------------------------------------------------------
rem  Starts the shared Obsidian MCP server that all your AIs use.
rem  Edit the settings below, then run register-startup.ps1 once.
rem ---------------------------------------------------------------

set "OBSIDIAN_VAULT_PATH=C:\Users\Faris\Desktop\MyVault"
rem obsidian-mcp.exe goes in the "bin" folder next to this "windows" folder
set "OBSIDIAN_MCP_EXE=%~dp0..\bin\obsidian-mcp.exe"
if not exist "%OBSIDIAN_MCP_EXE%" set "OBSIDIAN_MCP_EXE=%USERPROFILE%\.cargo\bin\obsidian-mcp.exe"

rem Keep backups and the inbox out of search results
set "OBSIDIAN_EXCLUDE_PATHS=_System/backups/**,00_Inbox/**"

rem Semantic search ("find by meaning")
set "OBSIDIAN_EMBEDDINGS=true"

rem --- OPTION A: built-in local embeddings on CPU.
rem     obsidian-mcp 3.1.0 could not load intfloat/multilingual-e5-small or BAAI/bge-m3
rem     (both failed with: model file 'onnx/model.onnx' not found).
rem set "OBSIDIAN_EMBEDDING_PROVIDER=local"
rem set "OBSIDIAN_EMBEDDINGS_MODEL=intfloat/multilingual-e5-small"

rem --- OPTION B: multilingual through LM Studio. bge-m3 is loaded with GPU off (0 GPU layers).
set "OBSIDIAN_EMBEDDING_PROVIDER=api"
set "OBSIDIAN_EMBEDDING_API_BASE=http://localhost:1234/v1"
set "OBSIDIAN_EMBEDDING_API_MODEL=text-embedding-bge-m3"
set "OBSIDIAN_EMBEDDING_API_KEY=unused"

"%OBSIDIAN_MCP_EXE%" --http
