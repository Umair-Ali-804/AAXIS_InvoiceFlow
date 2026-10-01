"""Run AAXIS InvoiceFlow locally:  python run.py  → http://localhost:8000"""
import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.main:app", host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8000")),
                reload=os.getenv("RELOAD", "0") == "1")
