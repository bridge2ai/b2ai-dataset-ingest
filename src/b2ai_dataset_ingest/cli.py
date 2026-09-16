"""Command-line interface for b2ai-dataset-ingest.

Usage (once implemented):

    b2ai-ingest voice \\
        --input data/synthetic/voice_dgp/b2ai-voice-synthetic-phenotype/output/phenotype \\
        --output out/ --target phenopacket

    b2ai-ingest aireadi \\
        --input data/synthetic/aireadi \\
        --output out/synthetic/aireadi/phenopackets

Synthetic input lives under ``data/synthetic/``; ``data/real/`` holds the source datasets
and may be owned by a separate account.
"""

from __future__ import annotations

import logging
from pathlib import Path

import typer

app = typer.Typer(
    add_completion=False,
    help="Ingest Bridge2AI datasets into GA4GH Phenopackets (and other future targets).",
)

# Registry of available output emitters, keyed by target name.
EMITTERS = {"phenopacket": "b2ai_dataset_ingest.emitters:PhenopacketEmitter"}


@app.command()
def voice(
    input: Path = typer.Option(..., "--input", "-i", help="Path to the voice phenotype/ dir."),
    output: Path = typer.Option(..., "--output", "-o", help="Output directory."),
    config: Path = typer.Option(
        Path("config/voice"), "--config", "-c", help="Mapping config dir."
    ),
    target: str = typer.Option("phenopacket", "--target", "-t", help="Output target."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Log per-table warnings."),
) -> None:
    """Ingest the Bridge2AI-Voice dataset into one phenopacket per participant."""
    from b2ai_dataset_ingest.emitters import PhenopacketEmitter
    from b2ai_dataset_ingest.sources.voice import VoiceSource

    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if target not in EMITTERS:
        typer.echo(f"unknown target {target!r}; available: {', '.join(EMITTERS)}", err=True)
        raise typer.Exit(code=2)
    if target != "phenopacket":  # only the phenopacket emitter is wired in v1
        typer.echo(f"target {target!r} is not implemented yet", err=True)
        raise typer.Exit(code=2)

    source = VoiceSource(root=input, config_dir=config)
    participants = list(source.read())
    written = PhenopacketEmitter().write_all(participants, output)
    typer.echo(f"Wrote {written} phenopackets to {output}")
    # Aggregate, PHI-safe summary so silent degradation (skipped items, un-keyed sessions,
    # unmapped tables) is visible rather than hidden behind a reassuring file count.
    typer.echo(source.report.render())


@app.command()
def aireadi(
    input: Path = typer.Option(..., "--input", "-i", help="Path to the AI-READI dataset/ dir."),
    output: Path = typer.Option(..., "--output", "-o", help="Output directory."),
    config: Path = typer.Option(
        Path("config/aireadi"), "--config", "-c", help="Mapping config dir."
    ),
    target: str = typer.Option("phenopacket", "--target", "-t", help="Output target."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Log per-table warnings."),
) -> None:
    """Ingest the Bridge2AI AI-READI dataset (OMOP CDM) into one phenopacket per participant."""
    from b2ai_dataset_ingest.emitters import PhenopacketEmitter
    from b2ai_dataset_ingest.sources.aireadi import AireadiSource

    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if target not in EMITTERS:
        typer.echo(f"unknown target {target!r}; available: {', '.join(EMITTERS)}", err=True)
        raise typer.Exit(code=2)
    if target != "phenopacket":
        typer.echo(f"target {target!r} is not implemented yet", err=True)
        raise typer.Exit(code=2)

    source = AireadiSource(root=input, config_dir=config)
    # The generator is handed straight to the emitter: OMOP tables are large (the synthetic
    # measurement table is 768k rows) and write_all holds nothing.
    written = PhenopacketEmitter().write_all(source.read(), output)
    typer.echo(f"Wrote {written} phenopackets to {output}")
    typer.echo(source.report.render())


@app.command("validate-aireadi")
def validate_aireadi_cmd(
    input: Path = typer.Option(..., "--input", "-i", help="Path to the AI-READI dataset/ dir."),
    config: Path = typer.Option(
        Path("config/aireadi"), "--config", "-c", help="Mapping config dir."
    ),
    strict_coverage: bool = typer.Option(
        False,
        "--strict-coverage",
        help=(
            "Assert this release is COMPLETE: a configured item or table it does not ship "
            "becomes an error. Use against a full release. On a subset release a "
            "low-prevalence item is absent by sampling rather than by schema, so this will "
            "flag it -- see config/aireadi/conditions.yaml."
        ),
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Log details."),
) -> None:
    """Preflight-check the AI-READI OMOP layout against its configs.

    Reads only headers, item-key inventories and aggregate counts -- never a cell value, a
    ``person_id`` or a date -- so it is safe to run and to print against the licensed
    release. Exits non-zero if any contract error is found.
    """
    from b2ai_dataset_ingest.sources.aireadi.validate import validate_aireadi

    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    report = validate_aireadi(root=input, config_dir=config, strict_coverage=strict_coverage)
    typer.echo(report.render())
    if report.errors:
        raise typer.Exit(code=1)


@app.command()
def validate(
    input: Path = typer.Option(..., "--input", "-i", help="Path to the voice phenotype/ dir."),
    config: Path = typer.Option(
        Path("config/voice"), "--config", "-c", help="Mapping config dir."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Log per-table warnings."),
) -> None:
    """Preflight-check the voice layout against its configs and data dictionaries.

    Reads only headers, dictionary keys, and aggregate cell counts — never raw values — so it
    is safe on the real, PHI-sensitive dataset. Exits non-zero if any contract error is found.
    """
    from b2ai_dataset_ingest.sources.voice.validate import validate_voice

    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    report = validate_voice(root=input, config_dir=config)
    typer.echo(report.render())
    if report.errors:
        raise typer.Exit(code=1)


@app.command("validate-mappings")
def validate_mappings(
    mappings: Path = typer.Option(
        Path("mappings"), "--mappings", "-m", help="Directory of *.sssom.tsv files."
    ),
    data_root: Path = typer.Option(
        None,
        "--data-root",
        "-d",
        help="phenotype/ dir to check b2ai: subjects against (skipped if omitted).",
    ),
    strict_ontology: bool = typer.Option(
        False,
        "--strict-ontology",
        help="Fail if the oaklib HPO backend is unavailable (default: skip the HPO check).",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Log details."),
) -> None:
    """Validate the B2AI -> HPO SSSOM mappings (no hallucinated / drifted HPO terms).

    Structural checks always run. The HPO existence/label check runs when oaklib + the HPO
    SQLite are available (install the ``validation`` extra); ``--strict-ontology`` makes their
    absence an error. Subject columns are checked only when ``--data-root`` is given. Exits
    non-zero if any error is found.
    """
    from b2ai_dataset_ingest.ontology.sssom_validate import default_mapping_files, validate_paths

    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    files = sorted(mappings.glob("*.sssom.tsv")) or default_mapping_files()
    if not files:
        typer.echo(f"no *.sssom.tsv files found under {mappings}", err=True)
        raise typer.Exit(code=2)
    result = validate_paths(
        files, data_root=data_root, check_ontology=True if strict_ontology else None
    )
    typer.echo(result.render())
    if result.errors:
        raise typer.Exit(code=1)


@app.command()
def targets() -> None:
    """List available output targets."""
    for name in EMITTERS:
        typer.echo(name)


def main() -> None:  # pragma: no cover - thin wrapper
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
