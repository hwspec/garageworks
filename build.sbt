// See README.md for license details.

ThisBuild / scalaVersion     := "2.13.18"
ThisBuild / version          := "0.4.0-SNAPSHOT"
ThisBuild / organization     := "io.github.hwspec"
ThisBuild / organizationName := "hwspec"
ThisBuild / versionScheme    := Some("early-semver")
ThisBuild / logLevel := Level.Warn
Test / logLevel := Level.Info

ThisBuild / homepage := Some(url("https://github.com/hwspec/garageworks"))
ThisBuild / licenses := Seq("BSD-3-Clause" -> url("https://opensource.org/licenses/BSD-3-Clause"))
ThisBuild / scmInfo := Some(
  ScmInfo(
    url("https://github.com/hwspec/garageworks"),
    "scm:git@github.com:hwspec/garageworks.git",
  )
)
ThisBuild / developers := List(
  Developer(
    id = "kazutomo",
    name = "Kazutomo Yoshii",
    email = "kazutomo@anl.gov",
    url = url("https://github.com/kazutomo"),
  )
)

ThisBuild / sonatypeCredentialHost := "central.sonatype.com"
ThisBuild / publishTo := {
  if (isSnapshot.value)
    Some("central-snapshots" at "https://central.sonatype.com/repository/maven-snapshots/")
  else sonatypePublishToBundle.value
}
ThisBuild / publishMavenStyle := true

val chiselVersion = "7.13.0"
val scalatestVersion = "3.2.19"

lazy val garageworks = (project in file("."))
  .settings(
    name := "garageworks",
    description := "Lightweight FPGA testing framework and AXI utilities for Chisel.",
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
    // cmdtemplate is the integration test harness for the Axi4Lite/BFM classes,
    // so it stays compiled and tested in src/, but it's excluded from the published artifacts.
    Compile / packageBin / mappings ~= {
      _.filterNot { case (_, path) => path.startsWith("cmdtemplate/") }
    },
    Compile / packageSrc / mappings ~= {
      _.filterNot { case (_, path) => path.startsWith("cmdtemplate/") }
    },
  )
