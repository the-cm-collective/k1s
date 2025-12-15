make bench-mem-combine GLOB='snapshots/*/*'
make bench-mem-plot CSV=combined/combined.csv OUTDIR=charts
make docs
