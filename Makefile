.PHONY: setup setup-ai run test clean

setup:
	python -m venv .venv
	. .venv/bin/activate && pip install -r requirements.txt

setup-ai:
	. .venv/bin/activate && pip install -r requirements-ai.txt

run:
	. .venv/bin/activate && uvicorn app.main:app --host 127.0.0.1 --port 8000

test:
	. .venv/bin/activate && python -m unittest discover -s tests -v

clean:
	rm -rf __pycache__ app/__pycache__ tests/__pycache__ .pytest_cache
