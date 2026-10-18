import os

COUNT = int(os.environ.get("SMOKE_SAMPLES", "10"))

rule all:
    input:
        expand("results/{i}.txt", i=range(COUNT))

rule task:
    output:
        "results/{i}.txt"
    resources:
        queue="mock", mem_mb=64, runtime=1
    shell:
        "printf '%s\\n' '{wildcards.i}' > {output}"
