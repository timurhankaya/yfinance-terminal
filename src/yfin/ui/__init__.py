"""The web terminal: a browser UI served by the API process under /ui.

Nothing here is imported unless `YFAPI_UI_ENABLED` is on -- `create_app`
guards the import -- so a deployment that has not opted in carries no
UI code path at all.
"""
