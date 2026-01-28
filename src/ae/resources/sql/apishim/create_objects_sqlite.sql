
                CREATE TABLE IF NOT EXISTS objects (
                  grp TEXT NOT NULL,
                  ver TEXT NOT NULL,
                  res TEXT NOT NULL,
                  ns  TEXT NOT NULL,
                  name TEXT NOT NULL,
                  metadata TEXT NOT NULL,
                  spec TEXT NOT NULL,
                  status TEXT NOT NULL,
                  rv INTEGER NOT NULL,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL,
                  PRIMARY KEY (grp, ver, res, ns, name)
                )
                