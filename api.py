from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from mangum import Mangum
import os

from fin_analyst.config import load_settings
from fin_analyst.llm import ClaudeAnalyst
from fin_analyst.passages import fetch_passages
from fin_analyst.graph import run_analysis

app = FastAPI(title="Fin Analyst API")

class AnalysisRequest(BaseModel):
    ticker: str
    question: str
    peer: str | None = None
    no_text: bool = False
    no_verify: bool = False

class AnalysisResponse(BaseModel):
    memo: str
    cost_usd: float

@app.post("/analyze", response_model=AnalysisResponse)
async def analyze(request: AnalysisRequest):
    settings = load_settings()
    
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY environment variable is not set.")
    
    analyst = ClaudeAnalyst(settings)
    fetch_text = None if request.no_text else fetch_passages
    
    try:
        # Run the agent synchronously
        state = run_analysis(
            request.ticker.upper(), 
            request.question, 
            analyst, 
            settings, 
            peer_ticker=request.peer.upper() if request.peer else None,
            fetch_text=fetch_text, 
            verify=not request.no_verify,
            quote=None  # Can be mapped if ALPHAVANTAGE_KEY is configured
        )
        
        if not state.memo:
            raise HTTPException(status_code=500, detail=f"Agent failed to generate memo: {state.error}")
            
        return AnalysisResponse(memo=state.memo, cost_usd=analyst.spent_usd)
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Mangum acts as the bridge between API Gateway (or Lambda Function URL) and FastAPI
handler = Mangum(app)
