import os

from invoke import Context, task

WINDOWS = os.name == "nt"
PROJECT_NAME = "oracle_mnist"
PYTHON_VERSION = "3.11"


# docker commands
@task
def build_train(ctx: Context, progress: str = "plain") -> None:
    """Build docker image for training."""
    ctx.run(f"docker build -t train:latest . -f dockerfiles/train.dockerfile --progress={progress}",
            echo=True,
            pty=not WINDOWS)
    

@task
def build_api(ctx: Context, progress: str = "plain") -> None:
    """Build docker images."""
    ctx.run(
        f"invoke build_train --progress {progress}",
        echo=True,
        pty=not WINDOWS,
    )
    ctx.run(
        f"docker build -t api:latest . -f dockerfiles/api.dockerfile --progress={progress}", echo=True, pty=not WINDOWS
    )


@task
def train_docker(ctx: Context, no_gpu: bool=False) -> None:
    """Run training docker container."""

    command = [
        "docker",
        "run",
        "--rm",
        "--mount type=bind,src=./configs/,dst=/workspace/configs", # Mount the configs directory
        "--mount type=bind,src=./lightning_logs/,dst=/workspace/lightning_logs", # Mount the lightning_logs directory
        "--mount type=bind,src=./outputs/,dst=/workspace/outputs", # Mount the outputs directory
    ]

    if not no_gpu:
        command.append("--gpus all")
    
    command.append("train:latest")
    ctx.run(" ".join(command), echo=True, pty=not WINDOWS)


# Setup commands
@task
def create_environment(ctx: Context) -> None:
    """Create a new conda environment for project."""
    ctx.run(
        f"conda create --name {PROJECT_NAME} python={PYTHON_VERSION} pip --no-default-packages --yes",
        echo=True,
        pty=not WINDOWS,
    )


@task
def requirements(ctx: Context) -> None:
    """Install project requirements."""
    ctx.run("pip install -U pip setuptools wheel", echo=True, pty=not WINDOWS)
    ctx.run("pip install -r requirements.txt", echo=True, pty=not WINDOWS)
    ctx.run("pip install -e .", echo=True, pty=not WINDOWS)


@task(requirements)
def dev_requirements(ctx: Context) -> None:
    """Install development requirements."""
    ctx.run('pip install -e .["dev"]', echo=True, pty=not WINDOWS)


# Project commands
@task
def preprocess_data(ctx: Context) -> None:
    """Preprocess data."""
    ctx.run(
        f"python src/{PROJECT_NAME}/data.py",
        echo=True,
        pty=not WINDOWS,
    )

@task
def test(ctx: Context) -> None:
    """Run tests with coverage."""
    ctx.run(
        "coverage run --source=src -m unittest discover -s tests -p 'test_*.py'",
        echo=True,
        pty=not WINDOWS,
    )
    ctx.run("coverage report -m", echo=True, pty=not WINDOWS)



# Documentation commands
@task(dev_requirements)
def build_docs(ctx: Context) -> None:
    """Build documentation."""
    ctx.run(
        "mkdocs build --config-file docs/mkdocs.yaml --site-dir build",
        echo=True,
        pty=not WINDOWS,
    )


@task(dev_requirements)
def serve_docs(ctx: Context) -> None:
    """Serve documentation."""
    ctx.run("mkdocs serve --config-file docs/mkdocs.yaml", echo=True, pty=not WINDOWS)
