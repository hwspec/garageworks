// See README.md for license details.

ThisBuild / scalaVersion     := "2.13.18"
ThisBuild / version          := "0.3.0"
ThisBuild / organization     := "io.github.hwspec"
ThisBuild / logLevel := Level.Warn

val chiselVersion = "7.13.0"
val scalatestVersion = "3.2.19"

lazy val garageworks = (project in file("."))
  .settings(
    name := "garageworks",
    libraryDependencies ++= Seq(
      "org.chipsalliance" %% "chisel" % chiselVersion,
      "org.scalatest" %% "scalatest" % scalatestVersion % "test",
    ),
    scalacOptions ++= Seq(
      "-language:reflectiveCalls",
      "-deprecation",
      "-feature",
      "-Xcheckinit",
      "-Ymacro-annotations",
    ),
    addCompilerPlugin("org.chipsalliance" % "chisel-plugin" % chiselVersion cross CrossVersion.full),
  )
