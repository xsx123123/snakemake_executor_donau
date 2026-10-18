rule all:
    input:
        "results/a.txt", "results/b.txt"

rule slow:
    output:
        "results/{sample}.txt"
    resources:
        queue="mock", mem_mb=64, runtime=1
    shell:
        "sleep 12; printf '%s\\n' '{wildcards.sample}' > {output}"
